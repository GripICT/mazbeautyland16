# See LICENSE file for full copyright and licensing details.

from odoo import models, _
from odoo.exceptions import UserError


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def get_categories(self, integration):
        if integration.is_prestashop():
            categories = list()

            for category in self.public_categ_ids:
                categories_list = category.parse_parent_external_recursively(integration)
                categories.extend(categories_list)

            return list(set(categories))

        return super(ProductTemplate, self).get_categories(integration)

    def get_related_products(self, integration):
        return [
            x.to_external_or_export(integration) for x in self.optional_product_ids
        ]

    def get_product_cost_template(self, integration):
        self.ensure_one()

        cost = self._get_template_price_from_seller(integration)
        return cost

    def _get_template_price_from_seller(self, integration):
        cost = float()
        sellers = self.seller_ids.filtered(
            lambda s:
            s.name.active and (not s.company_id or s.company_id == integration.company_id)
        )
        sorted_sellers = sellers.sorted(lambda s: (s.sequence, -s.min_qty, s.price, s.id))

        for seller in sorted_sellers:
            cost = seller.price
            if cost:
                break
        else:
            cost = self.standard_price

        return cost

    def get_in_stock_delivery_message(self, integration):
        self.ensure_one()

        result = integration.convert_translated_field_to_integration_format(
            integration, 'message_templame_in_stock'
        )

        if not integration.product_delivery_in_stock:
            raise UserError(_('In-stock Product Delivery Days field is not specified'))

        days = int(getattr(self, integration.product_delivery_in_stock.name))

        if not isinstance(result, dict):
            result = result.format(days)
        else:
            result = {key: value.format(days) for key, value in result.items()}

        return result

    def get_out_of_stock_delivery_message(self, integration):
        self.ensure_one()

        result = integration.convert_translated_field_to_integration_format(
            integration, 'message_templame_out_of_stock'
        )

        if not integration.product_delivery_out_of_stock:
            raise UserError(_('Out-of-stock Product Delivery Days field is not specified'))

        days = int(getattr(self, integration.product_delivery_out_of_stock.name))

        if not isinstance(result, dict):
            result = result.format(days)
        else:
            result = {key: value.format(days) for key, value in result.items()}

        return result
