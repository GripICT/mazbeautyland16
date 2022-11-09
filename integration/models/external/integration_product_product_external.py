# See LICENSE file for full copyright and licensing details.

from odoo import models, fields, _
from odoo.exceptions import UserError

import logging

_logger = logging.getLogger(__name__)


class IntegrationProductProductExternal(models.Model):
    _name = 'integration.product.product.external'
    _inherit = 'integration.external.mixin'
    _description = 'Integration Product Product External'
    _odoo_model = 'product.product'

    external_product_template_id = fields.Many2one(
        comodel_name='integration.product.template.external',
        string='External Product Template',
        readonly=True,
        ondelete='cascade',
    )

    def import_stock_levels(self, qty, location):
        self.ensure_one()

        variant = self.mapping_model.to_odoo(
            integration=self.integration_id,
            code=self.code,
        )

        if variant.type != 'product' or variant.tracking != 'none':
            return

        StockQuant = self.env['stock.quant'].with_context(skip_inventory_export=True)

        # Set stock levels to zero
        inventory_locations = self.env['stock.location'].search([
            ('parent_path', 'like', location.parent_path + '%'),
            ('id', '!=', location.id)
        ])

        inventory_quants = StockQuant.search([
            ('location_id', 'in', inventory_locations.ids),
            ('product_id', '=', variant.id),
        ])

        inventory_quants.inventory_quantity = 0
        inventory_quants.action_apply_inventory()

        # Set new stock level
        inventory_quant = StockQuant.search([
            ('location_id', '=', location.id),
            ('product_id', '=', variant.id),
        ])

        if not inventory_quant:
            inventory_quant = StockQuant.create({
                'location_id': location.id,
                'product_id': variant.id,
            })

        inventory_quant.inventory_quantity = float(qty)
        inventory_quant.action_apply_inventory()

    def _fix_unmapped(self, adapter_external_data):
        # We can't use this method, because products are imported by blocks
        pass

    def _post_import_external_one(self, adapter_external_record):
        """
        This method will receive individual variant record.
        And link external variant with external template.
        """
        template_code = adapter_external_record.get('ext_product_template_id')
        if not template_code:
            raise UserError(
                _('External Product Variant should have "ext_product_template_id" field')
            )

        external_template = self.env['integration.product.template.external'].search([
            ('code', '=', template_code),
            ('integration_id', '=', self.integration_id.id),
        ])

        if not external_template:
            raise UserError(
                _('No External Product Template found with code %s. '
                  'Maybe templates are not exported yet?') % template_code
            )

        assert len(external_template) == 1  # just to doublecheck, as it should never happen
        self.external_product_template_id = external_template.id
