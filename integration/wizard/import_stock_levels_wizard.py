# See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models


class ImportStockLevelsWizard(models.TransientModel):
    _name = 'import.stock.levels.wizard'
    _description = 'Import Stock Levels Wizard'

    location_id = fields.Many2one(
        string='Specify Location to import Stock',
        comodel_name='stock.location',
        domain=lambda self: [
            ('company_id', '=', self._get_company_id()),
            ('usage', '=', 'internal')
        ],
        required=True,
    )

    @api.model
    def _get_company_id(self):
        integration = self._get_sale_integration()
        return integration.company_id.id if integration else None

    @api.model
    def _get_sale_integration(self):
        integration = self.env['sale.integration'].browse(self._context.get('active_ids'))
        return integration[0] if integration else None

    def run_import_by_blocks(self, stock_levels, integration):
        ProductProductExternal = self.env['integration.product.product.external']

        for variant_code, qty in stock_levels:
            variant_external = ProductProductExternal.get_external_by_code(
                integration,
                variant_code,
                raise_error=False
            )

            if variant_external:
                variant_external = variant_external.with_context(
                    company_id=integration.company_id.id
                )
                variant_external.with_delay(
                    description='Import Stock Levels. Import for Single Product '
                ).import_stock_levels(qty, self.location_id)

    def run_import(self):
        integration = self._get_sale_integration()
        limit = integration.get_external_block_limit()
        adapter = integration._build_adapter()

        stock_levels = adapter.get_stock_levels()
        stock_levels = [(key, value) for key, value in stock_levels.items()]

        while stock_levels:
            self.with_delay(
                description='Import Stock Levels: Prepare Products'
            ).run_import_by_blocks(stock_levels[:limit], integration)

            stock_levels = stock_levels[limit:]
