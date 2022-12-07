# See LICENSE file for full copyright and licensing details.

from ...exceptions import NoReferenceFieldDefined, NoExternal, ApiImportError
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.osv import expression
from odoo.tools.sql import escape_psql

import logging

_logger = logging.getLogger(__name__)

RESULT_CREATED = 1
RESULT_ALREADY_MAPPED = 2
RESULT_MAPPED = 3
RESULT_EXISTS = 4
RESULT_NOT_IN_EXTERNAL = 5


class IntegrationExternalMixin(models.AbstractModel):
    _name = 'integration.external.mixin'
    _description = 'Integration External Mixin'
    _odoo_model = None

    integration_id = fields.Many2one(
        comodel_name='sale.integration',
        required=True,
        ondelete='cascade',
    )
    company_id = fields.Many2one(
        related='integration_id.company_id',
    )
    type_api = fields.Selection(
        related='integration_id.type_api',
    )
    code = fields.Char(
        required=True,
    )
    name = fields.Char(
        string='External Name',
        help='Contains name of the External Object in selected Integration',
    )
    external_reference = fields.Char(
        string='External Reference',
        help='Contains unique code of the External Object in the external '
             'system. Used for automated mapping',
    )

    _sql_constraints = [
        (
            'uniq_code',
            'unique(integration_id, code)',
            'Code should be unique',
        ),
        (
            'uniq_reference',
            'unique(integration_id, external_reference)',
            'External Reference should be unique',
        ),
    ]

    @property
    def odoo_model(self):
        assert bool(self._odoo_model), 'Class attribute `_odoo_model` not defined'
        return self.env[self._odoo_model]

    @property
    def mapping_model(self):
        assert bool(self._odoo_model), 'Class attribute `_odoo_model` not defined'
        return self.env[f'integration.{self._odoo_model}.mapping']

    def write(self, vals):
        result = super().write(vals)
        self.requeue_jobs_if_needed()
        return result

    @api.model_create_multi
    def create(self, vals):
        result = super().create(vals)
        result.requeue_jobs_if_needed()
        return result

    def requeue_jobs_if_needed(self):
        QueueJob = self.env['queue.job']

        for external in self:
            if external.external_reference:
                QueueJob.requeue_integration_jobs(
                    'NoExternal',
                    external._name,
                    external.code,
                )

    @api.model
    def create_or_update(self, vals):
        domain = [
            ('integration_id', '=', vals['integration_id']),
            ('code', '=', vals['code']),
        ]

        record = self.search(domain, limit=1)
        if record:
            record.write(vals)
            return record
        return self.create(vals)

    def name_get(self):
        result = []
        for rec in self:
            name = getattr(rec, rec._rec_name)
            if rec.external_reference:
                result.append((rec.id, '(%s)[%s] %s'
                               % (rec.code, rec.external_reference, name)))
            else:
                result.append((rec.id, '(%s) %s' % (rec.code, name)))
        return result

    @api.model
    def _name_search(self, name='', args=None, operator='ilike', limit=100, name_get_uid=None):
        args = args or []
        if operator == 'ilike' and not (name or '').strip():
            domain = []
        else:
            domain = ['|', ('name', operator, name), ('code', operator, name)]

        return self._search(
            expression.AND([domain, args]), limit=limit, access_rights_uid=name_get_uid,
        )

    def _map_external(self, adapter_external_data):
        if not self:
            return False

        for rec in self:
            rec.try_map_by_external_reference()

        return self._fix_unmapped(adapter_external_data)

    def try_map_by_external_reference(self, odoo_search_domain=False):
        self.ensure_one()

        odoo_model = self.odoo_model
        reference_field_name = getattr(odoo_model, '_internal_reference_field', None)
        if not reference_field_name:
            raise NoReferenceFieldDefined(
                _('No _internal_reference_field field defined for model %s') % self._name
            )

        # If we found existing mapping, we do not need to do anything
        odoo_id = odoo_model.from_external(
            self.integration_id,
            self.code,
            raise_error=False,
        )
        if odoo_id:
            return

        odoo_object = None
        if self.external_reference:
            search_domain = [(reference_field_name, '=ilike', escape_psql(self.external_reference))]
            # We can redefine domain if we need it
            if odoo_search_domain:
                search_domain = odoo_search_domain
            odoo_object = odoo_model.search(search_domain)
            if len(odoo_object) > 1:
                # If found more than one object we need to skip
                odoo_object = None

        odoo_model.create_or_update_mapping(self.integration_id, odoo_object, self)

    def _fix_unmapped(self, adapter_external_data):
        # Method that should be overriden in needed external models
        pass

    def create_integration_external(self, odoo_record, extra_vals=None):
        """Integration External --> Odoo"""
        self.ensure_one()

        odoo_record.create_mapping(
            self.integration_id,
            self.code,
            extra_vals=extra_vals,
        )

    @api.model
    def get_external_by_code(self, integration, code, raise_error=True):
        external = self.search([
            ('integration_id', '=', integration.id),
            ('code', '=', code),
        ])

        if raise_error:
            if not external:
                raise NoExternal(
                    _('Can not find external record', self._name, code, integration)
                )

            if len(external) > 1:
                raise NoExternal(
                    _('Found several external records', self._name, code, integration)
                )

        return external

    # Expecting value
    # {'language': [
    #     {
    #         'attrs': {'id': '1'},
    #         'value': 'Wood Panel'
    #     },
    #     {
    #         'attrs': {'id': '2'},
    #         'value': 'Wood france'
    #     },
    # ]}
    @api.model
    def get_original_name(self, value, integration=None):
        integration = integration or self.integration_id
        default_language_id = integration.get_settings_value('language_id')
        languages = value['language']

        if isinstance(languages, dict):
            languages = [languages]

        default_language = [x for x in languages if x['attrs']['id'] == default_language_id]

        if default_language:
            return default_language[0]['value']
        else:
            raise ApiImportError(
                "Can't find default language id in list of translations from e-Commerce System"
            )

    @api.model
    def create_or_update_with_translation(self, integration, odoo_object, vals):
        translatable_fields = {}
        regular_fields = {}
        lang_code = self.env.lang or 'en_US'

        for field, value in vals.items():
            if isinstance(value, dict) and value.get('language'):
                translatable_fields[field] = value
            else:
                regular_fields[field] = value

        # Create dictionary with languages and translations
        # {'external_lang': {
        #    'odoo_language_code': odoo_language_code,
        #    'vals': {field_name : value}
        # }}
        trans_dict = {}
        ResLang = self.env['res.lang']

        for field, value in translatable_fields.items():
            languages = value['language']

            if isinstance(languages, dict):
                languages = [languages]

            for language in languages:
                external_language_id = language['attrs']['id']

                if external_language_id not in trans_dict:
                    odoo_language = ResLang.from_external(integration, external_language_id)

                    # If  external language == current system language, update it in regular_fields
                    # Because of, we cant create object without required fields
                    if lang_code == odoo_language.code:
                        regular_fields.update({field: language['value']})
                        continue

                    trans_dict[external_language_id] = {
                        'odoo_language_code': odoo_language.code,
                        'vals': {}
                    }

                trans_dict[external_language_id]['vals'].update({field: language['value']})

        # Update fields without translations
        if odoo_object:
            odoo_object.update(regular_fields)
        else:
            odoo_object = odoo_object.create(regular_fields)

        # Update translations
        for upd_vals in trans_dict.values():
            odoo_object.with_context(lang=upd_vals['odoo_language_code']).update(upd_vals['vals'])

        return odoo_object

    def _pre_import_external_check(self, external_record, integration):
        return True

    def _post_import_external_one(self, adapter_external_record):
        """It's a hook method for redefining."""
        pass

    def _post_import_external_multi(self, adapter_external_record):
        """It's a hook method for redefining."""
        pass

    @api.model
    def _fix_unmapped_element(self, integration, element):
        # element - 'attribute' or 'feature'
        ElementValueMapping = self.env[f'integration.product.{element}.value.mapping']
        ExternalElement = self.env[f'integration.product.{element}.external']
        MappingElement = self.env[f'integration.product.{element}.mapping']
        ElementValue = self.env[f'product.{element}.value']

        external_values = getattr(integration._build_adapter(), f'get_{element}_values')()

        external_values_by_id = {
            x['id']: x['id_group'] for x in external_values
        }

        # 1. Try to find unmapped "Product Attribute/Feature Value Mapping"
        mapped_element_values = ElementValueMapping.search([
            ('integration_id', '=', integration.id),
            (element + '_value_id', '=', False),
        ])

        for mapped_element_value in mapped_element_values:
            # 2. Get "Product Attribute/Feature Value External"
            external_element_value = getattr(mapped_element_value, f'external_{element}_value_id')

            if not external_element_value:
                continue

            external_element_code = external_values_by_id.get(external_element_value.code, None)

            # 3. Get "Product Attribute/Feature External" by Code (External ID)
            external_element = ExternalElement.search([
                ('integration_id', '=', integration.id),
                ('code', '=', external_element_code)
            ])

            if not external_element:
                continue

            # 4. Get by mapping "Product Attribute/Feature" by Code (External ID)
            value = MappingElement.search([
                ('integration_id', '=', integration.id),
                (f'external_{element}_id', '=', external_element.id),
            ]).mapped(f'{element}_id')

            if not value or len(value) != 1:
                continue

            # 5. Get "Product Attribute/Feature Value" by Name
            product_element_value = ElementValue.search([
                (f'{element}_id', '=', value.id),
                ('name', '=ilike', escape_psql(external_element_value.name)),
            ])

            if product_element_value and len(product_element_value) == 1:
                # 6. Set attribute_value_id or feature_value_id
                setattr(mapped_element_value, element + '_value_id', product_element_value)

    def _post_import_external_element(self, adapter_external_record, element):
        """
        This method will receive individual attribute/feature value record.
        And link external attribute/feature value with external attribute/feature
        element - 'attribute' or 'feature'
        """
        # 1. Try to get Code (External ID) of Value
        element_code = adapter_external_record.get('id_group')
        if not element_code:
            raise UserError(_('External %s Value should have "%s" field.') % (
                element.capitalize(), 'id_group'
            ))

        # 2. Get "Product Attribute/Feature External" by Code (External ID)
        external_element = self.env[f'integration.product.{element}.external'].search([
            ('code', '=', element_code),
            ('integration_id', '=', self.integration_id.id),
        ])

        if not external_element:
            raise UserError(
                _('No External Product %s found with code %s. '
                  'Maybe %ss are not exported yet?') % (element.capitalize(), element_code, element)
            )

        assert len(external_element) == 1  # just to doublecheck, as it should never happen
        # 3. Set external_attribute_id or external_feature_id
        setattr(self, f'external_{element}_id', external_element.id)

    def _import_elements_and_values(self, ext_element, ext_values, element):
        result = {'element': 0, 'values': {
            RESULT_ALREADY_MAPPED: 0, RESULT_MAPPED: 0, RESULT_CREATED: 0}}
        MappingProductElement = self.env[f'integration.product.{element}.mapping']
        MappingProductElementValue = self.env[f'integration.product.{element}.value.mapping']
        ExternalProductElementValue = self.env[f'integration.product.{element}.value.external']
        ProductElement = self.env[f'product.{element}']
        ProductElementValue = self.env[f'product.{element}.value']

        # 1. Checks before creating
        element_mapping = MappingProductElement.get_mapping(self.integration_id, self.code)

        element_record = None
        # 1.1. Check that attribute/feature already mapped
        if element_mapping:
            element_record = getattr(element_mapping, f'{element}_id')

        odoo_object = ProductElement.search([('name', '=ilike', escape_psql(self.name))])

        # 1.2. Check by Name that attribute/feature already exists in Odoo
        if odoo_object and not element_record:
            result['element'] = RESULT_EXISTS
            return result

        # 2. Create Product Attribute/Feature (if it is not already created)
        if element_record:
            result['element'] = RESULT_ALREADY_MAPPED
        else:
            element_record = self.create_or_update_with_translation(
                integration=self.integration_id,
                odoo_object=ProductElement,
                vals={'name': ext_element['name']},
            )

            # Create mapping for new attribute
            MappingProductElement.create_or_update_mapping(
                self.integration_id,
                element_record,
                self,
            )

            result['element'] = RESULT_CREATED

        # 3. Create Product Attribute/Feature Values
        for ext_value in ext_values:
            # 4. Checks before creating
            element_value_mapping = \
                MappingProductElementValue.get_mapping(self.integration_id, ext_value['id'])

            element_value = None
            # 4.1. Check that attribute already mapped
            if element_value_mapping:
                element_value = getattr(element_value_mapping, f'{element}_value_id')

            if element_value:
                result['values'][RESULT_ALREADY_MAPPED] += 1
                continue

            # 5. Try to find "Product Attribute/Feature Value" by Name or create
            name = ext_value['name']
            if isinstance(name, dict) and name.get('language'):
                name = self.get_original_name(name)

            element_value = ProductElementValue.search([
                (f'{element}_id', '=', element_record.id),
                ('name', '=ilike', escape_psql(name)),
            ])

            if element_value:
                result['values'][RESULT_MAPPED] += 1
            else:
                element_value = self.create_or_update_with_translation(
                    integration=self.integration_id,
                    odoo_object=ProductElementValue,
                    vals={
                        'name': name,
                        f'{element}_id': element_record.id,
                    },
                )
                result['values'][RESULT_CREATED] += 1

            # 6.  Get external record and if it doesn't exists create it
            external_value = ExternalProductElementValue.get_external_by_code(
                self.integration_id,
                ext_value['id'],
                raise_error=False,
            )

            if not external_value:
                external_value = ExternalProductElementValue.create({
                    'code': ext_value['id'],
                    'name': element_value.name,
                    'integration_id': self.integration_id.id,
                })

            # 7. Create mapping for new product attribute/feature value
            MappingProductElementValue.create_or_update_mapping(
                self.integration_id,
                element_value,
                external_value,
            )

        return result

    def _run_import_elements_element(self, element):
        res_element = {}
        res_values = {}
        elements_by_integration = {}
        msg = ''

        # Distribute selected attributes/features by connectors
        for external_element in self:
            integration_id = external_element.integration_id.id

            if integration_id not in elements_by_integration:
                elements_by_integration[integration_id] = {
                    'integration': external_element.integration_id,
                    'elements': []
                }

            elements_by_integration[integration_id]['elements'] += [external_element]

        for integration_id, external_elements in elements_by_integration.items():
            adapter = external_elements['integration']._build_adapter()

            # Get attributes and values from External System
            ext_elements = getattr(adapter, f'get_{element}s')()
            ext_values = getattr(adapter, f'get_{element}_values')()

            # Create dict with selected attributes/features
            # and attributes/features + values from External System
            elements_dict = {
                external_element.code: {
                    'ext_elements': {},
                    'ext_values': [],
                    'external_element': external_element
                }
                for external_element in external_elements['elements']
            }

            for ext_element in ext_elements:
                if ext_element['id'] in elements_dict:
                    elements_dict[ext_element['id']]['ext_elements'] = ext_element

            for ext_value in ext_values:
                if ext_value['id_group'] in elements_dict:
                    elements_dict[ext_value['id_group']]['ext_values'] += [ext_value]

            # Run through the attributes and try to import them
            for key, item in elements_dict.items():
                external_element = item['external_element']

                if not item['ext_elements']:
                    result = {'element': RESULT_NOT_IN_EXTERNAL, 'values': {}}
                else:
                    result = external_element._import_elements_and_values(
                        item['ext_elements'],
                        item['ext_values'],
                        element,
                    )

                if result['element'] in (RESULT_ALREADY_MAPPED, RESULT_CREATED):
                    res_element[result['element']] = res_element.get(result['element'], 0) + 1
                else:
                    res_element[result['element']] = res_element.get(result['element'], []) + \
                        [external_element.name]

                for key, value_result in result['values'].items():
                    res_values[key] = res_values.get(key, 0) + value_result

        # Create message
        if res_element.get(RESULT_CREATED) or res_values.get(RESULT_CREATED):
            msg += _('\n\nImported:\n - Product %ss: %s\n - Product %s Values: %s') % (
                element.capitalize(),
                res_element.get(RESULT_CREATED, 0),
                element.capitalize(),
                res_values.get(RESULT_CREATED, 0),
            )

        if res_element.get(RESULT_ALREADY_MAPPED) or res_values.get(RESULT_ALREADY_MAPPED):
            msg += _('\n\nAlready mapped:\n - Product %ss: %s\n - Product %s Values: %s') % (
                element.capitalize(),
                res_element.get(RESULT_ALREADY_MAPPED, 0),
                element.capitalize(),
                res_values.get(RESULT_ALREADY_MAPPED, 0),
            )

        if res_element.get(RESULT_MAPPED):
            msg += _('\n\nProduct %ss Values mapped: %s') % (
                element.capitalize(), res_element.get(RESULT_MAPPED))

        if res_element.get(RESULT_EXISTS):
            msg += _('\n\nProduct %ss already existing in Odoo:\n - ') % element.capitalize()
            msg += '%s' % '\n - '.join(res_element.get(RESULT_EXISTS))

        if res_element.get(RESULT_NOT_IN_EXTERNAL):
            msg += _('\n\nProduct %ss that do not exist in e-Commerce System:\n - ') \
                % element.capitalize()
            msg += '%s' % '\n - '.join(res_element.get(RESULT_NOT_IN_EXTERNAL))

        message_id = self.env['message.wizard'].create({'message': msg[2:]})

        return {
            'name': _('Import Product %ss') % element.capitalize(),
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': 'message.wizard',
            'res_id': message_id.id,
            'target': 'new'
        }
