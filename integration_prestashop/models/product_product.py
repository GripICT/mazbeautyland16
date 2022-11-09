# See LICENSE file for full copyright and licensing details.

from odoo import models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def get_product_cost_variant(self, integration):
        self.ensure_one()
        return self.product_tmpl_id.get_product_cost_template(integration)
