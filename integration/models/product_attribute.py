# See LICENSE file for full copyright and licensing details.

from odoo import models


class ProductAttribute(models.Model):
    _name = 'product.attribute'
    _inherit = ['product.attribute', 'integration.model.mixin']
    _internal_reference_field = 'name'

    def export_with_integration(self, integration):
        self.ensure_one()
        return integration.export_attribute(self)

    def to_export_format(self, integration):
        self.ensure_one()

        return {
            'id': self.id,
            'name': integration.convert_translated_field_to_integration_format(
                self, 'name'
            ),
        }
