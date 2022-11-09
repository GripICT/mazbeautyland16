# See LICENSE file for full copyright and licensing details.

import base64
import json
from odoo import fields, models, api, _
from odoo.exceptions import UserError

TYPE_INPUT_FILE_SELECTION = [
    ('input_order', 'Input Order'),
    ('input_cancel_order', 'Input Cancel Order'),
    ('acknowledgement', 'Acknowledgement'),
    ('info', 'Info'),
    ('unknown', 'Unknown'),
]

TYPE_OUTPUT_FILE_SELECTION = [
    ('inventory', 'Inventory'),
    ('acknowledgement', 'Acknowledgement'),
    ('functional_acknowledgement', 'Functional Acknowledgement'),
    ('confirm_shipment', 'Confirm Shipment'),
    ('invoice', 'Invoice'),
    ('unknown', 'Unknown'),
]


class SaleIntegrationFile(models.Model):
    _name = 'sale.integration.file'
    _description = 'Sale Integration File'

    name = fields.Char(
        string='Name',
    )
    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('done', 'Done'),
            ('cancelled', 'Cancelled'),
            ('skipped', 'Skipped'),
            ('unknown', 'Unknown'),
        ],
        string='State',
        default='draft',
        readonly=True,
        copy=False,
    )
    si_id = fields.Many2one(
        'sale.integration',
        string='Service',
        required=True,
        ondelete='cascade',
        readonly=True,
        states={'draft': [('readonly', False)]},
    )
    file = fields.Binary(
        string='File',
    )

    order_id = fields.Many2one(
        string='Related Sales Order',
        comodel_name='sale.order',
        ondelete='set null',
    )

    raw_data = fields.Text(
        string='Raw Data in JSON',
        required=True,
        default='',
    )

    _order = 'create_date desc'

    _sql_constraints = [
        (
            'name_uniq', 'unique(si_id, name)',
            'Order name must be unique by partner!'
        )
    ]

    def action_cancel(self):
        orders = self.filtered(lambda s: s.state in ['draft', 'unknown'])
        return orders.write(
            {
                'state': 'cancelled',
            }
        )

    def action_draft(self):
        orders = self.filtered(lambda s: s.state in ['cancelled', 'done'] and not s.order_id)
        return orders.write(
            {
                'state': 'draft',
            }
        )


class SaleIntegrationInputFile(models.Model):
    _name = 'sale.integration.input.file'
    _inherit = 'sale.integration.file'
    _description = 'Sale Integration Input File'

    display_data = fields.Text(
        string='Raw Data',
        compute='_compute_display_data',
        inverse='_inverse_display_data',
    )

    order_reference = fields.Char(
        string='Order Reference',
        compute='_compute_order_reference',
        help='Reference received from the input file',
    )

    @api.model
    def create(self, vals_list):
        input_files = super(SaleIntegrationInputFile, self).create(vals_list)

        for input_file in input_files:
            input_file = input_file.with_context(company_id=input_file.si_id.company_id.id)
            input_file.with_delay(description='Run Process "Create Orders"').process()

        return input_files

    def _get_external_reference(self):
        try:
            data_dict = json.loads(self.raw_data)
            reference = data_dict.get('order', {}).get('reference')
        except json.decoder.JSONDecodeError:
            reference = ''
        return reference

    @api.depends('display_data')
    def _compute_order_reference(self):
        for input_file in self:
            input_file.order_reference = input_file._get_external_reference() or ''

    @api.depends('file', 'raw_data')
    def _compute_display_data(self):
        for input_file in self:
            try:
                input_file.display_data = json.dumps(
                    self.with_context(bin_size=False).to_dict(),
                    indent=4,
                )
            except json.decoder.JSONDecodeError:
                input_file.display_data = {}

    def _inverse_display_data(self):
        for input_file in self:
            try:
                json.loads(input_file.display_data)
                input_file.raw_data = input_file.display_data
            except json.decoder.JSONDecodeError as e:
                raise UserError(_('Incorrect file format:\n\n') + e.msg)

            input_file.raw_data = input_file.display_data

    def process(self):
        self.ensure_one()
        return self.env['sale.integration'].trigger_create_order(self)

    def to_dict(self):
        self.ensure_one()

        if self.raw_data:
            return json.loads(self.raw_data)
        else:
            json_str = base64.b64decode(self.file)
            return json.loads(json_str)

    def process_no_job(self):
        self.ensure_one()
        integration = self.si_id
        return integration.create_order_from_input(self)

    def run_export_tracking_no_job(self):
        if not self.order_id:
            return False
        return self.order_id.picking_ids._run_integration_picking_hooks()
