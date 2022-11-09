# See LICENSE file for full copyright and licensing details.

import logging

from odoo.tools.sql import escape_psql
from odoo.exceptions import UserError
from odoo import models, fields, _

from ...tools import IS_FALSE


_logger = logging.getLogger(__name__)


class IntegrationAccountTaxExternal(models.Model):
    _name = 'integration.account.tax.external'
    _inherit = 'integration.external.mixin'
    _description = 'Integration Account Tax External'
    _odoo_model = 'account.tax'

    external_tax_group_ids = fields.Many2many(
        comodel_name='integration.account.tax.group.external',
        relation='external_tax_group_to_external_tax_relation',
        column1='external_tax_id',
        column2='external_tax_group_id',
        string='Related External Tax Groups',
        readonly=True,
    )

    def _fix_unmapped(self, adapter_external_data):
        result = list()
        mapping_model = self.mapping_model

        for record, adapter_data in zip(self, adapter_external_data):
            mapping = mapping_model.search([
                ('external_tax_id', '=', record.id),
                ('integration_id', '=', record.integration_id.id),
            ], limit=1)

            if not mapping:
                continue

            odoo_record = mapping._fix_unmapped_tax_one(external_data=adapter_data)
            result.append(odoo_record)

        return result

    def try_map_by_external_reference(self, odoo_search_domain=False):
        self.ensure_one()

        odoo_model = self.odoo_model
        # If we found existing mapping, we do not need to do anything
        if odoo_model.from_external(self.integration_id, self.code, raise_error=False):
            return

        odoo_model.create_or_update_mapping(self.integration_id, None, self)

    def import_taxes(self):
        integrations = self.mapped('integration_id')

        for integration in integrations:
            external_values = integration._build_adapter().get_taxes()

            for tax in self.filtered(lambda x: x.integration_id == integration):
                tax.import_tax(external_values)

    def import_tax(self, external_values):
        self.ensure_one()

        Tax = self.odoo_model
        MappingTax = self.mapping_model

        # Try to find existing and mapped tax
        mapping = MappingTax.search([('external_tax_id', '=', self.id)])
        odoo_tax = Tax

        # If mapping doesn`t exists try to find tax by the name
        if not mapping or not mapping.tax_id:
            if Tax.search([('name', '=ilike', escape_psql(self.name))]):
                raise UserError(_('Tax with name "%s" already exists') % self.name)
        else:
            odoo_tax = mapping.tax_id

        # in case we only receive 1 record its not added to list as others
        if not isinstance(external_values, list):
            external_values = [external_values]

        # Find tax in external and children of our tax
        external_value = [x for x in external_values if x['id'] == self.code]

        if external_value:
            external_value = external_value[0]

            odoo_tax = self.create_or_update_with_translation(
                integration=self.integration_id,
                odoo_object=odoo_tax,
                vals={
                    'name': external_value['name'],
                    'amount': float(external_value.get('rate', IS_FALSE)),
                    'type_tax_use': 'sale',
                    'integration_id': self.integration_id.id,
                },
            )

            MappingTax.create_or_update_mapping(self.integration_id, odoo_tax, self)

    def _post_import_external_one(self, adapter_external_record):
        """
        This method will receive individual tax record.
        In case it has tax_groups - they will be created.
        """
        external_tax_groups = adapter_external_record.get('tax_groups')
        if not external_tax_groups:
            return

        all_tax_groups = []
        ExternalTaxGroup = self.env['integration.account.tax.group.external']

        for external_tax_group in external_tax_groups:
            tax_group = ExternalTaxGroup.create_or_update({
                'integration_id': self.integration_id.id,
                'code': external_tax_group['id'],
                'name': external_tax_group['name'],
                'external_reference': external_tax_group.get('external_reference'),
            })
            all_tax_groups.append((4, tax_group.id, 0))

        if all_tax_groups:
            self.external_tax_group_ids = all_tax_groups
