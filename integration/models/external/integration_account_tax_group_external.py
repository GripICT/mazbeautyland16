# See LICENSE file for full copyright and licensing details.

from odoo import models, fields, _
from odoo.exceptions import UserError
from odoo.tools.sql import escape_psql
import logging

_logger = logging.getLogger(__name__)


class IntegrationAccountTaxGroupExternal(models.Model):
    _name = 'integration.account.tax.group.external'
    _inherit = 'integration.external.mixin'
    _description = 'Integration Account Tax Group External'
    _order = 'sequence, id'
    _odoo_model = 'account.tax.group'

    sequence = fields.Integer(
        string='Priority',
        default=10,
        readonly=True,
    )

    external_tax_ids = fields.Many2many(
        comodel_name='integration.account.tax.external',
        relation='external_tax_group_to_external_tax_relation',
        column1='external_tax_group_id',
        column2='external_tax_id',
        string='Related External Taxes',
        readonly=True,
    )

    default_external_tax_id = fields.Many2one(
        comodel_name='integration.account.tax.external',
        string='Default External Tax',
    )

    def try_map_by_external_reference(self, odoo_search_domain=False):
        self.ensure_one()

        odoo_model = self.odoo_model
        # If we found existing mapping, we do not need to do anything
        if odoo_model.from_external(self.integration_id, self.code, raise_error=False):
            return

        odoo_model.create_or_update_mapping(self.integration_id, None, self)

    def import_tax_group(self, external_values):
        self.ensure_one()

        TaxGroup = self.odoo_model
        MappingTaxGroup = self.mapping_model

        # Try to find existing and mapped tax group
        mapping = MappingTaxGroup.search([('external_tax_group_id', '=', self.id)])
        odoo_tax_group = TaxGroup

        # If mapping doesn`t exists try to find tax group by the name
        if not mapping or not mapping.tax_group_id:
            if TaxGroup.search([('name', '=ilike', escape_psql(self.name))]):
                raise UserError(_('Tax group with name "%s" already exists') % self.name)
        else:
            odoo_tax_group = mapping.tax_group_id

        # in case we only receive 1 record its not added to list as others
        if not isinstance(external_values, list):
            external_values = [external_values]

        # Find tax group in external and children of our tax group
        external_value = [x for x in external_values if x['id'] == self.code]

        if external_value:
            external_value = external_value[0]

            odoo_tax_group = self.create_or_update_with_translation(
                integration=self.integration_id,
                odoo_object=odoo_tax_group,
                vals={'name': external_value['name']},
            )

            MappingTaxGroup.create_or_update_mapping(self.integration_id, odoo_tax_group, self)
