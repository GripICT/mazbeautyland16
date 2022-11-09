# See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class IntegrationProductAttributeValueMapping(models.Model):
    _name = 'integration.product.attribute.value.mapping'
    _inherit = 'integration.mapping.mixin'
    _description = 'Integration Product Attribute Value Mapping'
    _mapping_fields = ('attribute_value_id', 'external_attribute_value_id')

    attribute_value_id = fields.Many2one(
        comodel_name='product.attribute.value',
        ondelete='cascade',
    )

    external_attribute_value_id = fields.Many2one(
        comodel_name='integration.product.attribute.value.external',
        required=True,
        ondelete='cascade',
    )
