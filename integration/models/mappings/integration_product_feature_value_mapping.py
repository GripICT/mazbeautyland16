# See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class IntegrationProductFeatureValueMapping(models.Model):
    _name = 'integration.product.feature.value.mapping'
    _inherit = 'integration.mapping.mixin'
    _description = 'Integration Product Feature Value Mapping'
    _mapping_fields = ('feature_value_id', 'external_feature_value_id')

    feature_value_id = fields.Many2one(
        comodel_name='product.feature.value',
        ondelete='cascade',
    )

    external_feature_value_id = fields.Many2one(
        comodel_name='integration.product.feature.value.external',
        required=True,
        ondelete='cascade',
    )
