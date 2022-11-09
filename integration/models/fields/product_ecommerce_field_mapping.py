# See LICENSE file for full copyright and licensing details.

from odoo import models, fields


class ProductEcommerceFieldMapping(models.Model):
    _name = 'product.ecommerce.field.mapping'
    _description = 'Product Fields Integration Mapping'

    name = fields.Char(
        related='ecommerce_field_id.name',
        readonly=True,
        store=True,
    )

    ecommerce_field_id = fields.Many2one(
        comodel_name='product.ecommerce.field',
        required=True,
        domain='[("type_api","=",integration_api_type)]',
        ondelete='cascade',
    )

    integration_api_type = fields.Selection(
        related='integration_id.type_api',
        readonly=True,
        store=True,
    )

    integration_id = fields.Many2one(
        comodel_name='sale.integration',
        required=True,
        ondelete='cascade',
    )

    technical_name = fields.Char(
        related='ecommerce_field_id.technical_name',
        readonly=True,
        store=True,
    )

    odoo_model_id = fields.Many2one(
        comodel_name='ir.model',
        related='ecommerce_field_id.odoo_model_id',
        readonly=True,
        store=True,
    )

    odoo_field_id = fields.Many2one(
        comodel_name='ir.model.fields',
        related='ecommerce_field_id.odoo_field_id',
        readonly=True,
        store=True,
    )

    send_on_update = fields.Boolean(
        string='Send field for updating',
        default=False,
        help='By default fields that are available in the fields mapping will be ALL used '
             'to create new product record on external e-commerce system. But after record '
             'is created, we do not want to mess up and override changes that are done for '
             'that field on external system. Hence we can specify here if that field will be '
             'sent when updating product. ',
    )
