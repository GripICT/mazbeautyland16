# See LICENSE file for full copyright and licensing details.

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools.sql import escape_psql

import logging

_logger = logging.getLogger(__name__)


class IntegrationProductPublicCategoryExternal(models.Model):
    _name = 'integration.product.public.category.external'
    _inherit = 'integration.external.mixin'
    _description = 'Integration Product Public Category External'
    _rec_name = 'complete_name'
    _order = 'complete_name'
    _odoo_model = 'product.public.category'

    parent_id = fields.Many2one(
        comodel_name=_name,
        string='Parent Category',
        ondelete='cascade',
    )
    complete_name = fields.Char(
        string='Complete Name',
        compute='_compute_complete_name',
        recursive=True,
        store=True,
    )

    @api.depends('name', 'parent_id.complete_name')
    def _compute_complete_name(self):
        for category in self:
            name = category.name

            if category.parent_id:
                name = f'{category.parent_id.complete_name} / {name}'

            category.complete_name = name

    def _post_import_external_multi(self, adapter_external_records):
        adapter_router = {str(x['id']): x for x in adapter_external_records}
        self_router = {x.code: x for x in self}

        for rec in self:
            adapter_record = adapter_router.get(rec.code, dict())
            parent_id = adapter_record.get('id_parent')

            if parent_id:
                external_parent_record = self_router.get(parent_id, False)
                rec.parent_id = external_parent_record

    def try_map_by_external_reference(self, odoo_search_domain=False):
        self.ensure_one()

        odoo_model = self.odoo_model
        reference_field_name = getattr(odoo_model, '_internal_reference_field', None)

        # If we found existing mapping, we do not need to do anything
        odoo_id = odoo_model.from_external(
            self.integration_id,
            self.code,
            raise_error=False
        )
        if odoo_id:
            return

        odoo_object = None
        if self.name:
            odoo_object = odoo_model.search(
                [(reference_field_name, '=ilike', escape_psql(self.name))])
            if len(odoo_object) > 1:
                # If found more than one object we need to skip
                odoo_object = None

        odoo_model.create_or_update_mapping(self.integration_id, odoo_object, self)

    def _fix_unmapped(self, adapter_external_data):
        ProductPublicCategory = self.odoo_model

        if ProductPublicCategory.search([]):
            return

        category_mappings = []
        integration = self.integration_id
        external_values = integration._build_adapter().get_categories()

        # Create categories
        for external_value in external_values:
            external_product_category = self.get_external_by_code(
                integration,
                external_value['id'],
                raise_error=False,
            )

            category = self.create_or_update_with_translation(
                integration=integration,
                odoo_object=ProductPublicCategory,
                vals={'name': external_value['name']},
            )

            category_mappings += ProductPublicCategory.create_or_update_mapping(
                integration,
                category,
                external_product_category
            )

        # Create tree
        category_by_code = {}

        category_by_code.update({
            x.external_public_category_id.code: x.public_category_id
            for x in category_mappings
        })

        # in case we only receive 1 record its not added to list as others
        if not isinstance(external_values, list):
            external_values = [external_values]

        for ext_category in external_values:
            parent_id = ext_category.get('id_parent', None)
            category = category_by_code.get(ext_category['id'], None)
            if category and parent_id:
                if not category.parent_id:
                    category.parent_id = category_by_code.get(parent_id, None)

    def import_categories(self):
        integrations = self.mapped('integration_id')

        for integration in integrations:
            # Import categories from e-Commerce System
            external_values = integration._build_adapter().get_categories()

            for category in self.filtered(lambda x: x.integration_id == integration):
                category.import_category(external_values)

    def import_category(self, external_values):
        self.ensure_one()

        ProductCategory = self.odoo_model
        MappingCategory = self.mapping_model

        # Try to find existing and mapped category
        mapping = MappingCategory.search([
            ('external_public_category_id', '=', self.id),
        ])

        # If mapping doesn`t exists try to find category by the name
        if not mapping or not mapping.public_category_id:
            self._check_similar_in_odoo()
            odoo_category = ProductCategory
        else:
            assert len(mapping) == 1, _('Expected one mapping to one external record.')
            odoo_category = mapping.public_category_id

        # in case we only receive 1 record its not added to list as others
        if not isinstance(external_values, list):
            external_values = [external_values]

        # Find category in external and children of our category
        external_value = [x for x in external_values if x['id'] == self.code]
        external_children = [x for x in external_values if x.get('id_parent') == self.code]

        if external_value:
            external_value = external_value[0]

            odoo_category = self.create_or_update_with_translation(
                integration=self.integration_id,
                odoo_object=odoo_category,
                vals={'name': external_value['name']},
            )

            ProductCategory.create_or_update_mapping(self.integration_id, odoo_category, self)

            # Set parent of our category
            if external_value.get('id_parent'):
                parent_mapping = MappingCategory.get_mapping(
                    self.integration_id,
                    external_value['id_parent']
                )

                if parent_mapping and parent_mapping.public_category_id:
                    odoo_category.parent_id = parent_mapping.public_category_id

            # Find children and set parent to them
            for external_child in external_children:
                child_mapping = MappingCategory.get_mapping(
                    self.integration_id,
                    external_child['id']
                )

                if child_mapping and child_mapping.public_category_id:
                    child_mapping.public_category_id.parent_id = odoo_category

    def _check_similar_in_odoo(self):
        odoo_category = self.odoo_model.search([
            ('name', '=ilike', escape_psql(self.name)),
        ])

        if odoo_category:
            external_complete_name = self.complete_name
            odoo_category_records = odoo_category.browse()

            for category in odoo_category:
                odoo_parent_path = '/ '.join(category.parents_and_self.mapped('name'))

                if odoo_parent_path == external_complete_name:
                    odoo_category_records |= category

            if len(odoo_category_records) == 1:
                message = _('Public category with name "%s" already exists') % self.name
            elif len(odoo_category_records) > 1:
                message = _('There are several public categories with name "%s"') % self.name
            else:
                message = False

            if message:
                raise UserError(message)
