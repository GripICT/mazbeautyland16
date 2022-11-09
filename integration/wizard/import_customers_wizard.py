# See LICENSE file for full copyright and licensing details.

from odoo.exceptions import UserError
from odoo import models, fields, _

from ..models.integration_sale_order_factory import OTHER


class ImportCustomersWizard(models.TransientModel):
    _name = 'import.customers.wizard'
    _description = 'Import Customers Wizard'

    date_since = fields.Datetime(
        string='Import Customers Since',
        required=True,
        default=fields.Datetime.today(),
    )

    @staticmethod
    def _not_defined_from_context():
        return _('Integration may not be defined from context.')

    def _get_sale_integration(self):
        integration = self.env['sale.integration'].browse(self._context.get('active_ids'))

        if len(integration) > 1:
            raise UserError(self._not_defined_from_context())
        elif not integration.exists():
            raise UserError(self._not_defined_from_context())

        return integration

    def run_import(self):
        integration = self._get_sale_integration()
        limit = integration.get_external_block_limit()
        adapter = integration._build_adapter()
        customer_ids = adapter.get_customer_ids(self.date_since)

        customers = list()
        while customer_ids:
            partners = self.with_delay(
                description='Import Customers: Prepare Customers',
            ).run_import_by_blocks(customer_ids[:limit], integration)

            customers.append(partners)
            customer_ids = customer_ids[limit:]

        return customers

    def run_import_by_blocks(self, customer_ids, integration):
        self = self.with_context(company_id=integration.company_id.id)

        customers = list()
        for customer_id in customer_ids:
            partners = self.with_delay(
                description='Import Customers. Import for Single Customer',
            ).import_single_customer(customer_id, integration)
            customers.append(partners)
        return customers

    def import_single_customer(self, customer_id, integration):
        adapter = integration._build_adapter()
        factory = self.env['integration.sale.order.factory']

        customer, addresses = adapter.get_customer_and_addresses(customer_id)
        odoo_customer = factory._fetch_odoo_partner(integration, customer)

        partners = [odoo_customer]
        for address in addresses:
            partner = factory._fetch_odoo_partner(
                integration=integration,
                partner_data=address,
                address_type=address.get('address_type', OTHER),
                parent_id=odoo_customer.id if odoo_customer else False,
            )
            partners.append(partner)
        return partners
