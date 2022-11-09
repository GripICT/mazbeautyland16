#  Copyright 2020 VentorTech OU
#  License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

from odoo import fields, models, api, _


class SaleIntegration(models.Model):
    _inherit = 'sale.integration'

    order_name_ref = fields.Char(string='Sales Order prefix')

    _sql_constraints = [
        (
            'order_name_ref_unique',
            'unique(order_name_ref)',
            _('Sale Order prefix name should be unique.')
        ),
    ]

    @api.model
    def default_get(self, default_fields):
        # Because fields may not be created when installing
        # We have to override default_get to prefill them
        values = super(SaleIntegration, self).default_get(default_fields)

        values['so_delivery_note_field'] = \
            self.env.ref('integration.field_sale_order__integration_delivery_note').id
        values['picking_delivery_note_field'] = \
            self.env.ref('stock.field_stock_picking__note').id

        return values

    so_external_reference_field = fields.Many2one(
        string='Additional SO Number field on Sales Order',
        comodel_name='ir.model.fields',
        ondelete='cascade',
        help='Define here field name belonging to Sales Order (only Char and '
             'Text fields accepted) where integration will place sales order number'
             ' in addition to existing "External Sales Order Ref" field.',
        required=False,
        domain='[("store", "=", True), '
               '("model_id.model", "=", "sale.order"), '
               '("ttype", "in", ("text", "char")) ]',
    )

    so_delivery_note_field = fields.Many2one(
        string='Delivery Note field on Sales Order',
        comodel_name='ir.model.fields',
        ondelete='cascade',
        help='Define here field name belonging to Sales Order (only Char and '
             'Text fields accepted) where integration will place Delivery Note'
             ' text that is downloaded from target e-commerce system. This value '
             'is defaulting to the value defined in e-Commerce tab on Sales Order.'
             ' But it is allowed to define any field even from 3rd-party modules.',
        required=True,
        domain='[("store", "=", True), '
               '("model_id.model", "=", "sale.order"), '
               '("ttype", "in", ("text", "char")) ]',
    )

    picking_delivery_note_field = fields.Many2one(
        string='Delivery Note field on Picking',
        comodel_name='ir.model.fields',
        ondelete='cascade',
        help='Define here field name belonging to Stock Picking (only Char and '
               'Text fields accepted) where integration will place Delivery Note'
               ' text that is downloaded from target e-commerce system. This value '
               'is defaulting to the standard field \'Note\' defined on Stock Picking.'
               ' But it is allowed to define any field even from 3rd-party modules.',
        required=True,
        domain='[("store", "=", True), '
               '("model_id.model", "=", "stock.picking"), '
               '("ttype", "in", ("text", "char")) ]',
    )

    default_sales_team_id = fields.Many2one(
        string='Force Sales Team',
        comodel_name='crm.team',
        help='If set, this Sales Team will be automatically set to all '
             'Sales Orders coming from the e-Commerce System',
        check_company=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]"
    )

    default_sales_person_id = fields.Many2one(
        string='Force Sales Person',
        comodel_name='res.users',
        help='Sales Person to be assigned to new orders by default.',
        check_company=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]"
    )

    customer_company_vat_field = fields.Many2one(
        string='VAT/Reg. Number field on Company',
        comodel_name='ir.model.fields',
        ondelete='cascade',
        help='Define here field name belonging to Company (only Char fields '
             'accepted) where integration will place Company VAT/Registration Number'
             ' value that is downloaded from target e-commerce system. This value '
             'is defaulting to VAT field on the Company. But it is allowed to define any '
             'field even from 3rd-party modules.',
        required=False,
        domain='[("store", "=", True), '
               '("model_id.model", "=", "res.partner"), '
               '("ttype", "=", "char") ]',
    )

    customer_company_vat_field_name = fields.Char(related="customer_company_vat_field.name")

    customer_personal_id_field = fields.Many2one(
        string='Personal ID field on Contact',
        comodel_name='ir.model.fields',
        ondelete='cascade',
        help='Define here field name belonging to Contact (only Char fields '
             'accepted) where integration will place Personal ID Number'
             ' value that is downloaded from target e-commerce system. '
             'For example, needed for Italian localization.',
        required=False,
        domain='[("store", "=", True), '
               '("model_id.model", "=", "res.partner"), '
               '("ttype", "=", "char") ]',
    )

    default_customer = fields.Many2one(
        string='Default Customer',
        comodel_name='res.partner',
    )
