# See LICENSE file for full copyright and licensing details.

from odoo import api, models


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    @api.model_create_multi
    def create(self, vals_list):
        quants = super(StockQuant, self).create(vals_list)
        quants.trigger_export()
        return quants

    def write(self, vals):
        result = super(StockQuant, self).write(vals)
        self.trigger_export()
        return result

    def trigger_export(self):
        if self.env.context.get('skip_inventory_export'):
            return

        templates = self._get_templates_to_export_inventory()

        for template in templates:
            integrations = self.env['sale.integration'].get_integrations(
                'export_inventory',
                template.company_id,
            )

            variant_integrations = template.product_variant_ids.mapped('integration_ids')
            required_integrations = integrations.filtered(lambda x: x in variant_integrations)
            products = template.product_variant_ids

            for integration in required_integrations:
                key = f'export_inventory_{integration.id}_{template.id}'
                products.export_inventory_by_jobs(integration, key)

    def _get_templates_to_export_inventory(self):
        return (
            self.product_id.product_tmpl_id
            + self.product_id.get_used_in_kits_recursively()
        )
