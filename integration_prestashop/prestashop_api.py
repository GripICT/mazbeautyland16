# See LICENSE file for full copyright and licensing details.

import json
import itertools
import logging
from decimal import Decimal
from collections import defaultdict, Counter
from itertools import groupby

from prestapyt import PrestaShopWebServiceError

from odoo import _
from odoo.addons.integration.api.abstract_apiclient import AbsApiClient
from odoo.addons.integration.tools import TemplateHub
from odoo.addons.integration.tools import IS_TRUE, IS_FALSE
from odoo.exceptions import UserError
from odoo.tools import frozendict
from ..integration.exceptions import ApiImportError

from .presta import Client, PRESTASHOP  # noqa
from .presta.base_model import BaseModel


_logger = logging.getLogger(__name__)


PRODUCT_TYPE_MAP = {
    'simple': 'product',
    'pack': 'product',
    'virtual': 'service',
}
ROOT_CMS_PAGE_CATEGORY_ID = '1'


# TODO: all reading through pagination
class PrestaShopApiClient(AbsApiClient):

    default_receive_orders_filter = (
        '{"filter[current_state]": "[<put state id here>]"'
        ', "filter[date_upd]": ">[%s]" % record.presta_last_receive_orders_datetime}'
    )

    settings_fields = (
        ('url', 'Shop URL', ''),
        ('admin_url', 'Admin URL', ''),
        ('key', 'Webservice Key', ''),
        ('language_id', 'Language ID', ''),
        (
            'receive_orders_filter',
            'Receive Orders Filter',
            default_receive_orders_filter,
            True,
        ),
        ('import_products_filter', 'Import Products Filter', '{"active": "1"}'),
        ('id_group_shop', 'Shop Group where export products', ''),
        ('shop_ids', 'Shop ids in id_group_shop separated by comma', ''),
        (
            'PS_TIMEZONE',
            (
                'PrestaShop timezone value.'
                ' Will be automatically populated when integration is active'
            ),
            '',
        ),
    )

    def __init__(self, settings):
        super().__init__(settings)

        api_url = '/'.join([self.get_settings_value('url').strip('/'), 'api'])
        api_key = self.get_settings_value('key')

        admin_url = self.get_settings_value('admin_url').strip('/')
        if not admin_url.endswith('index.php'):
            admin_url += '/index.php'
        self.admin_url = admin_url

        self._client = Client(
            api_url,
            api_key,
        )

        self._language_id = self.get_settings_value('language_id')

        id_group_shop = self.get_settings_value('id_group_shop')

        shop_ids_str = self.get_settings_value('shop_ids')
        if shop_ids_str:
            shop_ids = shop_ids_str.split(',')
        else:
            shop_ids = []

        self._client.default_language_id = self._language_id
        self._client.id_group_shop = id_group_shop
        self._client.shop_ids = shop_ids
        self._client.data_block_size = self._settings['data_block_size']

    def check_connection(self):
        resources = self._client.get('')
        connection_ok = bool(resources)
        return connection_ok

    def get_api_resources(self):
        return self._client.get('')

    def get_delivery_methods(self):
        delivery_methods = self._client.model('carrier').search_read(
            filters={'deleted': IS_FALSE},
            fields=['id', 'name'],
        )

        return delivery_methods

    def get_single_carrier(self, external_id):
        carrier = self._client.model('carrier').search_read(
            filters={'id': external_id},
            fields=['id', 'name'],
        )
        return carrier and carrier[0] or dict()

    def get_parent_delivery_methods(self, not_mapped_id):
        id_reference = self._client.model('carrier').search_read(
            filters={'id': not_mapped_id},
            fields=['id_reference'],
        )
        reference = id_reference and id_reference[0]
        if not reference:
            return False
        family_delivery_methods = self._client.model('carrier').search_read(
            filters={'id_reference': reference['id_reference']},
            fields=['id', 'name'],
        )
        return family_delivery_methods

    def get_configuration(self, name):
        value = self._client.model('configuration').search_read(
            filters={'name': '[%s]' % name},
            fields=['value'],
            skip_translation=True,
        )

        return value and value[0] if isinstance(value, list) else value

    def get_single_tax(self, tax_id):
        taxes = self.get_taxes()
        tax_list = [x for x in taxes if x['id'] == tax_id]
        return tax_list and tax_list[0] or dict()

    def get_taxes(self):
        """Will return the following list:
            [
                {'id': '1',
                 'rate': '5.000',
                 'name': 'CA 5%',
                 'tax_groups': [{'id': '6', 'name': 'CA Standard Rate'}]
                 },
                 {'id': '3',
                  'rate': '0.000',
                  'name': 'CA-MB 8%',
                  'tax_groups': [{'id': '6', 'name': 'CA Standard Rate'}]
                 },
                 ...
            ]
        """
        # first retrieve all taxes
        taxes = self._client.model('tax').search_read(
            filters={'deleted': IS_FALSE},
            fields=['id', 'name', 'rate'],
        )

        # retrieve tax groups and convert to dictionary for fast searching
        tax_groups = self._client.model('tax_rule_group').search_read(
            filters={'deleted': IS_FALSE},
            fields=['id', 'name'],
        )

        tax_groups_dict = dict((p['id'], p['name']) for p in tax_groups)

        # retrieve all tax rules
        tax_rules = self._client.model('tax_rule').search_read(
            filters={},
            fields=['id_tax_rules_group', 'id_tax'],
        )

        # create dictionary of tax groups per tax
        tax_to_tax_rule_dict = defaultdict(set)
        for tax_rule in tax_rules:
            tax_id = tax_rule['id_tax']
            tax_group_id = tax_rule['id_tax_rules_group']
            tax_group_name = tax_groups_dict.get(tax_group_id)
            if tax_group_name:
                tax_group_dict = frozendict(
                    {
                        'id': tax_group_id,
                        'name': tax_group_name,
                    }
                )
                tax_to_tax_rule_dict[tax_id].add(tax_group_dict)

        for tax in taxes:
            tax['tax_groups'] = list(tax_to_tax_rule_dict.get(tax['id'], []))

        return taxes

    @staticmethod
    def create_error_message(duplicate_dict, object_name):
        """
        Format of the return message: <name> (<normalized_code>) - found in Prestashop in records:
        [<iso_code_in_presta>] <name> (id=id_in_presta), ..., ...

        :param duplicate_dict: Dictionary with duplicates
        :param object_name: name of the object for which duplicates were searched for
        """
        duplicate_message = []

        for key, vals in duplicate_dict.items():
            name, code = key[1], key[0]

            dupl_list = [f'[{d.get("iso_code")}] {d.get("name")} (id={d.get("id")})' for d in vals]

            text_error = f'{name} ({code}) - ' + \
                         _('found in Prestashop in records: ') + f'{", ".join(dupl_list)}'

            duplicate_message.append(text_error)

        if duplicate_message:
            return _('Found duplicated active %s in Prestashop, '
                     'please, fix duplicates:\n') \
                % object_name + ', \n'.join(duplicate_message)

    def _find_duplicated_objects(self, converted_and_orig_list):
        """
        Search for duplicates in a tuple list (converted_record, original_record)

        :param converted_and_orig_list: list of tuples
        :return: dictionary with duplicates
        :return format: {(converted_code, converted_name): [{'id', 'iso_code', 'name'}, [dict],...]}
        """

        transformed_unique_key_list = []
        group_unique_key_dict = defaultdict(list)

        code_name_mapping = {}
        for converted, original in converted_and_orig_list:
            converted_reference = converted['external_reference']
            converted_name = code_name_mapping.get(converted_reference)
            if not converted_name:
                converted_name = converted['name']
                code_name_mapping[converted_reference] = converted_name
            converted_key = (converted_reference, converted_name)
            transformed_unique_key_list.append((converted_key, original))

        [
            group_unique_key_dict[key].extend(list(i[1] for i in group))
            for key, group in groupby(
                transformed_unique_key_list,
                key=lambda tuple_: tuple_[0])
        ]

        return {key: val for key, val in group_unique_key_dict.items() if len(val) >= 2}

    def get_countries(self):
        countries = self._client.model('country').search_read(
            filters={'active': IS_TRUE},
            fields=['id', 'name', 'iso_code'],
        )

        converted_countries = []
        for external_country in countries:
            converted_countries.append({
                'id': external_country.get('id'),
                'name': external_country.get('name'),
                'external_reference': external_country.get('iso_code'),
            })

        converted_and_orig_list = [
            (obj_conv, obj_orig)for obj_conv, obj_orig in zip(converted_countries, countries)
        ]
        duplicates_dict = self._find_duplicated_objects(converted_and_orig_list)
        error_duplicate_message = self.create_error_message(duplicates_dict, 'countries')
        if error_duplicate_message:
            raise ApiImportError(error_duplicate_message)

        return converted_countries

    def get_states(self):
        countries = self._client.model('country').search_read(
            filters={'active': IS_TRUE},
            fields=['id', 'name', 'iso_code'],
        )

        original_state, converted_states = [], []
        for external_country in countries:
            states = self._client.model('state').search_read(
                filters={'id_country': external_country['id'], 'active': IS_TRUE},
                fields=['id', 'name', 'iso_code'],
            )

            original_state.extend(states)

            for external_state in states:
                state_code = external_state.get('iso_code')
                if '-' in state_code:
                    # In prestashop some states are defined in a strange way like (NL-DE)
                    # We need to have only state code
                    state_code = state_code.split('-')[1]
                state_reference = '{}_{}'.format(
                    external_country.get('iso_code'),
                    state_code
                )

                converted_states.append({
                    'id': external_state.get('id'),
                    'name': external_state.get('name'),
                    'external_reference': state_reference,
                })

        converted_and_orig_list = [
            (obj_conv, obj_orig) for obj_conv, obj_orig in zip(converted_states, original_state)
        ]
        duplicates_dict = self._find_duplicated_objects(converted_and_orig_list)
        error_duplicate_message = self.create_error_message(duplicates_dict, 'states')
        if error_duplicate_message:
            raise ApiImportError(error_duplicate_message)

        return converted_states

    def _convert_to_settings_language(self, field):  # TODO: maybe delete?
        result = BaseModel._get_translation(field, self._language_id)
        return result

    def get_payment_methods(self):
        orders = self._client.model('order').search_read(
            filters={},
            fields=['id', 'payment'],
            sort='[id_DESC]',
            limit=1000,
        )

        return [{'id': x['payment']} for x in orders]

    def get_languages(self):  # TODO: not optimal, better to use id_shop: all
        # TODO: if there is only one records presta returns it as simple {} rather than
        #  list of dicts [{}]
        added_language_ids = []
        languages = []

        ext_languages = self._client.get(
            'languages',
            options={
                'filter[active]': IS_TRUE,
                'display': '[id,name,language_code]',
            }
        )['languages']['language']

        if not isinstance(ext_languages, list):
            ext_languages = [ext_languages]

        for ext_language in ext_languages:
            if ext_language['id'] not in added_language_ids:
                language_code = ext_language.get('language_code', '').replace('-', '_')
                languages.append({
                    'id': ext_language.get('id'),
                    'name': ext_language.get('name'),
                    # Converting language code to Odoo Format
                    'external_reference': language_code,
                })
                added_language_ids.append(ext_language['id'])

        return languages

    def get_attributes(self):
        product_options = self._client.get(
            'product_options', options={'display': '[id,name]'}
        )['product_options']

        if not product_options:
            return []

        attributes = product_options['product_option']
        if not isinstance(attributes, list):
            attributes = [attributes]

        return attributes

    def get_attribute_values(self):
        product_option_values = self._client.get(
            'product_option_values', options={'display': '[id,name,id_attribute_group]'}
        )['product_option_values']

        if not product_option_values:
            return []

        product_option_values = product_option_values['product_option_value']
        if not isinstance(product_option_values, list):
            product_option_values = [product_option_values]

        return [
            {
                'id': x['id'],
                'name': x['name'],
                'id_group': x['id_attribute_group'],
            }
            for x in product_option_values
        ]

    def get_features(self):
        product_features = self._client.get(
            'product_features', options={'display': '[id,name]'}
        )['product_features']

        if not product_features:
            return []

        product_features = product_features['product_feature']
        if not isinstance(product_features, list):
            product_features = [product_features]

        return product_features

    def get_feature_values(self):
        product_feature_values = self._client.get(
            resource='product_feature_values',
            options={'display': '[id,value,id_feature]', 'filter[id_feature]': '![0]'},
        )['product_feature_values']

        if not product_feature_values:
            return []

        product_feature_values = product_feature_values['product_feature_value']
        if not isinstance(product_feature_values, list):
            product_feature_values = [product_feature_values]

        return [
            {
                'id': x['id'],
                'name': x['value'],
                'id_group': x['id_feature'],
            }
            for x in product_feature_values
        ]

    def get_categories(self):
        # Filter out category with ID = 1. See below code form Presta
        # Core/Domain/CmsPageCategory/ValueObject/CmsPageCategoryId.php#L39
        # public const ROOT_CMS_PAGE_CATEGORY_ID = 1;
        categories = self._client.model('category').search_read(
            filters={'active': IS_TRUE, 'id': '![%s]' % ROOT_CMS_PAGE_CATEGORY_ID},
            fields=['id', 'name', 'id_parent', 'is_root_category'],
            skip_translation=True,
        )

        result_categories = []
        for cat in categories:
            if cat['id_parent'] == ROOT_CMS_PAGE_CATEGORY_ID:
                cat['id_parent'] = IS_FALSE
            result_categories.append(cat)

        return result_categories

    def get_sale_order_statuses(self):
        external_order_states = self._client.model('order_state').search_read(
            filters={'deleted': 0},
            fields=['id', 'name', 'template'],
            skip_translation=True,
        )

        order_states = []

        for ext_order_state in external_order_states:
            order_states.append({
                'id': ext_order_state.get('id'),
                'name': ext_order_state.get('name'),
                # We cannot add any unique internal reference as in Prestashop it doesn't exist
                'external_reference': False,
            })

        return order_states

    def get_product_template_ids(self):
        template_ids = self._client.model('product').search_read_by_blocks(
            filters=self._get_product_filter_hook(),
            fields=self._get_product_fields_hook(['id']),
        )

        template_ids = self._filter_templates_hook(template_ids)

        return template_ids and [x['id'] for x in template_ids] or []

    def get_product_templates(self, template_ids):
        product_templates = self._client.model('product').search_read(
            filters={'id': '[%s]' % '|'.join(template_ids)},
            fields=['id', 'name', 'reference', 'ean13'],
        )

        product_variants = self._client.model('combination').search_read(
            filters={'id_product': '[%s]' % '|'.join(template_ids)},
        )

        for product_template in product_templates:
            reference = product_template['reference'] if product_template['reference'] else None
            product_template.update({
                'external_reference': reference,
                'barcode': product_template['ean13'],
                'variants': [],
            })

        result = {x['id']: x for x in product_templates}

        for product_variant in product_variants:
            product = result.get(product_variant['id_product'])
            if product:
                reference = product_variant['reference'] if product_variant['reference'] else None
                attribute_value_ids = product_variant.get('associations', {})\
                    .get('product_option_values', {}).get('product_option_value', [])

                if not isinstance(attribute_value_ids, list):
                    attribute_value_ids = [attribute_value_ids]

                product['variants'].append({
                    'id': '{product_id}-{combination_id}'.format(**{
                        'product_id': product_variant['id_product'],
                        'combination_id': product_variant['id'],
                    }),
                    'name': product['name'],
                    'external_reference': reference,
                    'ext_product_template_id': product_variant['id_product'],
                    'barcode': product_variant['ean13'],
                    'attribute_value_ids': [x['id'] for x in attribute_value_ids],
                })

        return result

    def get_customer_ids(self, date_since=None):
        customer_ids = self._client.model('customer').search_read_by_blocks(
            filters=date_since and {'date_upd': '>[%s]' % date_since},
            fields=['id'],
            date='1',
        )

        return customer_ids and [x['id'] for x in customer_ids] or []

    def get_customer_and_addresses(self, customer_id):
        customer = self._client.get('customers', customer_id)['customer']
        addresses = self._client.model('address').search_read(
            filters={'id_customer': '[%s]' % customer_id}
        )

        if not isinstance(addresses, list):
            addresses = [addresses]

        parsed_customer = self._parse_customer(customer)
        parsed_addresses = [self._parse_address(customer, x) for x in addresses]

        return parsed_customer, parsed_addresses

    def create_webhooks_from_routes(self, routes_dict):
        result = dict()

        for name_tuple, route in routes_dict.items():
            webhook = self._client.model('webhook')

            webhook.url = route
            webhook.hook = name_tuple[-1]  # --> technical_name
            webhook.real_time = IS_TRUE
            webhook.active = IS_TRUE
            webhook.retries = 0

            webhook.save()
            result[name_tuple] = str(webhook.id)

        return result

    def unlink_existing_webhooks(self, external_ids=None):
        if not external_ids:
            return False

        webhooks = self._client.model('webhook').search({
            'id': '[%s]' % '|'.join(external_ids)
        })
        result = webhooks.delete()
        return result

    def export_category(self, category):
        presta_category = self._client.model('category').create(category)
        return presta_category.id

    def find_existing_template(self, template):
        # we try to search existing product template ONLY if there is no external_id for it
        # If there is external ID then we already mapped products and we do not need to search
        if template['external_id']:
            return False

        # Now let's validate if there are no duplicated references in Prestashop
        product_refs = [str(x['reference']) for x in template['products']]
        tmpl_hub = self.get_templates_and_products_for_validation_test(product_refs)
        duplicated_ref = tmpl_hub.get_dupl_refs()
        if duplicated_ref:
            error_message = _('Duplicated references in Prestashop (below showed <reference>:'
                              '<product ids>) :\n\n')
            for ref, prod_ids in duplicated_ref.items():
                ids_str = ','.join(prod_ids)
                error_message += f'\t{ref} : {ids_str}\n'
            raise UserError(error_message)

        # Let's validate if all found products belong to the same product template
        ids_set = self._find_product_by_references(product_refs)

        # If nothing found, then just return False
        if len(ids_set) == 0:
            return False

        # If more than one product id found - then we found references,
        # but they all belong to different products and we need to inform user about it
        # So he can fix on Prestashop side
        # Because in Odoo it is single product template, and in Prestashop - separate
        # product templates. That should not be allowed. Note that after previous check on
        # duplicates most likely it will not be possible, this check is just to be 100% sure
        if len(ids_set) > 1:
            error_message = _('Product reference(s) "%s" were found in multiple Prestashop '
                              'Products: %s. This is not allowed as in Odoo same references'
                              ' already belong to single product template and its variants. '
                              'Structure of Odoo products and Prestashop Products should be '
                              'the same!') % (', '.join(product_refs), ', '.join(list(ids_set)))
            raise UserError(error_message)

        presta_product_id = list(ids_set)[0]

        # Check if products in Odoo has the same amount of variants as in Prestashop
        product = self._client.model('product').get(presta_product_id)
        product_combinations = product.get_combinations()
        # counting expected variants excluding "virtual" variant
        template_variants_count = len([x for x in template['products'] if x['attribute_values']])
        if template_variants_count != len(product_combinations):
            raise UserError(
                _('Amount of combinations in Prestashop is %d. While amount in Odoo is %d. '
                  'Please, check the product with id %s in Prestashop and make sure it has the same'
                  ' amount of combinations as variants in Odoo (with enabled integration '
                  '"%s")') % (
                    len(product_combinations),
                    template_variants_count,
                    presta_product_id,
                    self.integration_name
                )
            )
        for combination in product_combinations:
            # Make sure that reference is set on the combination
            if str(combination.id) != IS_FALSE and not combination.reference:
                error_message = _('Product with id "%s" do not have references on '
                                  'all combinations. Please, add them and relaunch '
                                  'product export') % presta_product_id
                raise UserError(error_message)
            attribute_values_from_presta = combination.associations \
                .get('product_option_values', {}).get('product_option_value', [])
            if isinstance(attribute_values_from_presta, dict):
                attribute_values_from_presta = [attribute_values_from_presta]
            attribute_values_from_presta = [x['id'] for x in attribute_values_from_presta]
            attribute_values_from_odoo = list(
                filter(lambda x: x['reference'] == combination.reference, template['products'])
            )
            if len(attribute_values_from_odoo) == 0:
                error_message = \
                    _('There is no variant in Odoo with reference "%s" that corresponds to '
                      'Prestashop product %s') % (combination.reference, presta_product_id)
                raise UserError(error_message)
            attribute_values_from_odoo = \
                [x['external_id'] for x in attribute_values_from_odoo[0]['attribute_values']]
            if Counter(attribute_values_from_odoo) != Counter(attribute_values_from_presta):
                error_message = \
                    _('Prestashop Variant with reference %s has variant values %s. While same '
                      'Odoo Variant has attribute values %s. Products in Prestashop and Odoo '
                      'with the same reference should have the same combination of attributes.') \
                    % (
                        combination.reference,
                        attribute_values_from_presta,
                        attribute_values_from_odoo,
                    )
                raise UserError(error_message)

        return presta_product_id

    def _find_product_by_references(self, product_refs):
        if product_refs and not isinstance(product_refs, list):
            product_refs = [str(product_refs)]

        reference_filter = {'reference': '[%s]' % '|'.join(product_refs)}
        product_fields = ['id', 'name', 'reference']
        combination_fields = ['id', 'id_product', 'reference']
        templates, variants = self._get_products_and_variants(product_fields,
                                                              combination_fields,
                                                              reference_filter)

        ids_set = set()
        ids_set.update([str(x['id']) for x in templates])
        ids_set.update([str(x['id_product']) for x in variants])
        return ids_set

    def validate_template(self, template):
        mappings_to_delete = []

        # (1) if template with such external id exists?
        presta_product_id = template['external_id']
        if presta_product_id:
            presta_product = self._client.model('product').search_read(
                filters={'id': presta_product_id},
                fields=['id']
            )
            if len(presta_product) == 0:
                mappings_to_delete.append({
                    'model': 'product.template',
                    'external_id': str(presta_product_id),
                })

        # (2) if variant with such external id exists?
        for variant in template['products']:
            variant_ext_id = variant['external_id']
            if not variant_ext_id:
                continue
            product_id, presta_combination_id = variant_ext_id.split('-')
            if presta_combination_id == IS_FALSE:
                presta_combination_id = None
            if presta_combination_id:
                presta_combination = self._client.model('combination').search_read(
                    filters={'id': presta_combination_id},
                    fields=['id']
                )
                if len(presta_combination) == 0:
                    mappings_to_delete.append({
                        'model': 'product.product',
                        'external_id': str(variant_ext_id),
                    })
        return mappings_to_delete

    def export_template(self, template):
        mappings = []

        presta_product_id = template['external_id']
        product = self._client.model('product').get(presta_product_id)

        self._fill_product(product, template)

        # we save product type here, before save, because it
        # got overridden with incorrect type after save
        presta_product_type = product.type

        product.save()
        mappings.append({
            'model': 'product.template',
            'id': template['id'],
            'external_id': str(product.id),
        })

        if presta_product_type == 'pack':
            stock = self._client.model('stock_available').search({
                'id_product': product.id,
            })
            stock.out_of_stock = IS_TRUE
            stock.save()

        for variant in template['products']:
            combination_id = self._export_product(product.id, variant)
            mappings.append({
                'model': 'product.product',
                'id': variant['id'],
                'external_id': '%s-%s' % (product.id, combination_id),
            })

        return mappings

    def _export_template_custom_field_hook(self, presta_template, template_vals):
        # Method to extend when you would like to add custom fields in order
        # to export them to Prestashop from Odoo
        pass

    def _fill_product(self, product, vals):
        product.type = 'simple'
        product.state = IS_TRUE
        product.is_virtual = IS_FALSE

        if 'name' in vals:
            self._fill_translated_field(
                product, 'name', vals['name']
            )
        if 'description' in vals:
            self._fill_translated_field(
                product, 'description', vals['description']
            )
        if 'description_short' in vals:
            self._fill_translated_field(
                product, 'description_short', vals['description_short']
            )

        if 'meta_title' in vals:
            self._fill_translated_field(
                product, 'meta_title', vals['meta_title']
            )
        if 'meta_description' in vals:
            self._fill_translated_field(
                product, 'meta_description', vals['meta_description']
            )

        if 'delivery_in_stock' in vals:
            self._fill_translated_field(
                product, 'delivery_in_stock', vals['delivery_in_stock']
            )

        if 'delivery_out_stock' in vals:
            self._fill_translated_field(
                product, 'delivery_out_stock', vals['delivery_out_stock']
            )

        if 'price' in vals:
            product.price = round(vals['price'], product.PRESTASHOP_PRECISION)
            product.show_price = IS_TRUE

        if 'wholesale_price' in vals:
            wholesale_price = round(vals['wholesale_price'], product.PRESTASHOP_PRECISION)
            product.wholesale_price = wholesale_price

        if 'available_for_order' in vals:
            product.available_for_order = IS_TRUE if vals['available_for_order'] else IS_FALSE

        if 'active' in vals:
            product.active = IS_TRUE if vals['active'] else IS_FALSE

        if vals['type'] == 'service':
            product.type = 'virtual'
            product.is_virtual = IS_TRUE

        if vals['kits'] and len(vals['products']) <= 1:
            product.type = 'pack'
            kit = vals['kits'][0]
            bundle_products = []
            for component in kit['components']:
                bundle_products.append({
                    'id': component['product_id'],
                    'quantity': component['qty'],
                })

            product.product_bundle = bundle_products

        if 'id_category_default' in vals:
            # Setting to the root (Home) category if no category specified
            default_category = vals['id_category_default'] or IS_FALSE
            product.id_category_default = default_category

        if 'categories' in vals:
            categories_list = vals['categories']
            if vals.get('id_category_default'):
                categories_list.append(vals['id_category_default'])
            category_ids = [
                x for x in set(categories_list) if x != IS_FALSE
            ]
            categories = self._client.model('category').get(category_ids)
            product.categories = categories

        if 'product_features' in vals:
            product.product_features = vals['product_features']

        if 'related_products' in vals:
            accessories = [{'id': x} for x in vals['related_products']]
            product.accessories = accessories

        if 'id_tax_rules_group' in vals:
            if vals['id_tax_rules_group']:
                product.id_tax_rules_group = vals['id_tax_rules_group'][0]['tax_group_id']
            else:
                product.id_tax_rules_group = IS_FALSE

        # process bare/standard product
        if len(vals['products']) > 1:
            product.weight = 0
        elif vals['products']:
            odoo_product = vals['products'][0]
            if 'weight' in odoo_product:
                product.weight = odoo_product['weight']
            if 'reference' in odoo_product:
                product.reference = odoo_product['reference'] or ''
            if 'ean13' in odoo_product:
                product.ean13 = odoo_product['ean13'] or ''

        self._export_template_custom_field_hook(product, vals)

    def _fill_translated_field(self, model, field, value):
        for lang_id, translation in value.items():
            setattr(model.lang(lang_id), field, translation or '')

    def _export_product(self, presta_product_id, product):
        if product['external_id']:
            product_id, combination_id = product['external_id'].split('-')
            if combination_id == IS_FALSE:
                combination_id = None
        else:
            product_id, combination_id = presta_product_id, None  # TODO: refactor

        if combination_id:
            combination = self._client.model('combination').get(combination_id)
            self._fill_combination(combination, product, product_id)
            combination.save()
        else:
            combination = self._client.model('combination')
            prestashop_product = self._client.model('product').get(product_id)
            if product['attribute_values']:
                self._fill_combination(combination, product, product_id)
                combination = prestashop_product.add_combination(combination)
            else:
                combination.id = IS_FALSE  # todo: clarify
                pass  # update template with some values

        return combination.id

    def _export_variant_custom_field_hook(self, presta_variant, variant_vals):
        # Method to extend when you would like to add custom fields in order
        # to export product to Prestashop from Odoo
        pass

    def _fill_combination(self, combination, vals, product_id):
        combination.id_product = product_id

        if 'reference' in vals:
            combination.reference = vals['reference'] or ''

        if 'price' in vals:
            price = round(vals['price'], combination.PRESTASHOP_PRECISION)
            combination.price = price

        if 'wholesale_price' in vals:
            wholesale_price = round(vals['wholesale_price'], combination.PRESTASHOP_PRECISION)
            combination.wholesale_price = wholesale_price

        if 'weight' in vals:
            combination.weight = vals['weight']

        if 'ean13' in vals:
            combination.ean13 = vals['ean13'] or ''

        if not combination.minimal_quantity:
            combination.minimal_quantity = 1

        if vals['attribute_values']:
            attribute_value_ids = [x['external_id'] for x in vals['attribute_values']]
            product_option_values = self._client.model('product_option_value').get(
                attribute_value_ids,
            )
            combination.product_option_values = product_option_values

        self._export_variant_custom_field_hook(combination, vals)

    def export_images(self, images):  # todo: naming
        product_id = images['template']['id']
        variant = self._client.model('product').get(product_id)

        presta_images = variant.get_images()
        for image in presta_images:
            image.delete()

        template_default_image = images['template']['default']
        if template_default_image:
            default_image = template_default_image['data']
            variant.add_image(default_image)

        for extra_image in images['template']['extra']:
            extra_image_data = extra_image['data']
            variant.add_image(extra_image_data)

        for product in images['products']:
            product_default_image = product['default']
            if product_default_image:
                product_image = product_default_image['data']
                variant.add_image(product_image)

            for product_extra_image in product['extra']:
                product_extra_image_data = product_extra_image['data']
                variant.add_image(product_extra_image_data)

    def export_attribute(self, attribute):
        product_option = self._client.model('product_option')

        self._fill_translated_field(
            product_option,
            'name',
            attribute['name'],
        )

        self._fill_translated_field(
            product_option,
            'public_name',
            attribute['name'],
        )

        product_option.group_type = 'select'
        product_option.save()

        return product_option.id

    def export_attribute_value(self, attribute_value):
        product_option_value = self._client.model('product_option_value')

        self._fill_translated_field(
            product_option_value,
            'name',
            attribute_value['name'],
        )

        product_option_value.id_attribute_group = attribute_value['attribute']
        product_option_value.save()

        return product_option_value.id

    def export_feature(self, feature):
        product_feature = self._client.model('product_feature')

        self._fill_translated_field(
            product_feature,
            'name',
            feature['name'],
        )

        product_feature.save()

        return product_feature.id

    def export_feature_value(self, feature_value):
        product_feature_value = self._client.model('product_feature_value')

        self._fill_translated_field(
            product_feature_value,
            'value',
            feature_value['name'],
        )

        product_feature_value.id_feature = feature_value['feature_id']
        product_feature_value.save()

        return product_feature_value.id

    def receive_orders(self):
        options = {
            'date': IS_TRUE,
        }

        filters = self.get_settings_value('receive_orders_filter')
        evl = self._settings['fields']['receive_orders_filter']['eval']
        if evl and type(filters) is not dict:
            raise UserError(
                _('The receive_orders_filter of sale_integration must contain dict()')
            )

        if evl:
            options.update(
                filters
            )

        orders = self._client.get('orders', options=options)['orders']
        if orders:
            orders = orders['order']
        else:
            orders = []

        if not isinstance(orders, list):
            orders = [orders]

        input_files = []
        for order in orders:
            order_id = order['attrs']['id']
            data = self._get_input_file_data(order_id)
            input_file = {
                'id': order['attrs']['id'],
                'data': data,
            }
            input_files.append(input_file)

        return input_files

    def _get_messages_list(self, order_id):
        options = {
            'filter[id_order]': order_id,
        }
        messages = self._client.get('messages', options=options)['messages']
        if messages:
            messages = messages['message']
        else:
            messages = []

        if not isinstance(messages, list):
            messages = [messages]

        message_list = []
        for message in messages:
            message_id = message['attrs']['id']
            message_data = self._client.get(
                'messages', message_id
            )['message']
            message_list.append(message_data)

        return message_list

    def _get_input_file_data(self, order_id):
        order = self._client.get('orders', order_id)['order']

        try:
            customer = self._client.get('customers', order['id_customer'])['customer']
        except PrestaShopWebServiceError:
            customer = {}

        try:
            delivery_address = self._client.get(
                'addresses', order['id_address_delivery'])['address']
        except PrestaShopWebServiceError:
            delivery_address = {}

        try:
            invoice_address = self._client.get('addresses', order['id_address_invoice'])['address']
        except PrestaShopWebServiceError:
            invoice_address = {}

        input_file_data = {
            'order': order,
            'customer': customer,
            'delivery_address': delivery_address,
            'invoice_address': invoice_address,
            'messages': self._get_messages_list(order_id),
            'payment_transactions': self._get_payment_transactions(order['reference'])
        }
        return input_file_data

    def _get_carrier_tax_ids(self, carrier_id, country_id, state_id, postcode):
        if not carrier_id or carrier_id == IS_FALSE:
            return list(), IS_FALSE

        tax_rule_group = self._client.model('carrier').search_read(
            filters={'id': carrier_id},
            fields=['id_tax_rules_group'],
        )

        tax_rule_group_id = tax_rule_group and tax_rule_group[0]['id_tax_rules_group']['value']

        return self._get_taxes_by_tax_rule(tax_rule_group_id, country_id, state_id, postcode)

    def _get_taxes_by_tax_rule(self, tax_rule_group_id, country_id, state_id, postcode):
        # Based on https://github.com/
        #     PrestaShop/PrestaShop/blob/develop/classes/tax/TaxRulesTaxManager.php#L74

        # behavior:
        #     '0' - this tax only
        #     '1' - combine
        #     '2' - one after another
        first_row = True
        behavior = IS_FALSE
        tax_ids = list()

        if not tax_rule_group_id or not country_id \
                or tax_rule_group_id == IS_FALSE or country_id == IS_FALSE:
            return tax_ids, behavior

        tax_rules = self._client.get(
            'tax_rules',
            options={
                'filter[id_country]': f'[{country_id}]',
                'filter[id_state]': f'[0|{state_id}]',
                'filter[id_tax_rules_group]': f'[{tax_rule_group_id}]',
                'display': '[id,id_state,zipcode_from,zipcode_to,id_tax,behavior]',
                'sort': '[zipcode_from_DESC,zipcode_to_DESC,id_state_DESC]',
            }
        )

        tax_rules = tax_rules['tax_rules'] and tax_rules['tax_rules']['tax_rule']

        if not tax_rules:
            return tax_ids, behavior

        if isinstance(tax_rules, dict):
            tax_rules = [tax_rules]

        tax_rules = list(
            filter(
                lambda x: (x['zipcode_from'] <= postcode <= x['zipcode_to'])
                or (x['zipcode_to'] == IS_FALSE and x['zipcode_from'] in [IS_FALSE, postcode]),
                tax_rules,
            )
        )

        for rule in tax_rules:
            tax_ids.append(rule['id_tax'])

            if first_row:
                behavior = rule['behavior']
                first_row = False

            if rule['behavior'] == IS_FALSE:
                break

        return tax_ids, behavior

    def _get_payment_transactions(self, order_ref):
        payments = self._client.model('order_payment').search_read(
            filters={'order_reference': order_ref},
            fields=['amount', 'transaction_id', 'date_add', 'id_currency', 'payment_method'],
        )

        payment_transactions = []

        for payment in payments:
            if payment['transaction_id']:
                presta_currency = self._client.model('currency').search_read(
                    filters={
                        'id': payment['id_currency'],
                    },
                    fields=['iso_code'],
                )
                currency = presta_currency and presta_currency[0] or dict()
                transaction_vals = {
                    'transaction_id': '%s (%s): %s' % (order_ref,
                                                       payment['payment_method'],
                                                       payment['transaction_id']),
                    'transaction_date': payment['date_add'],
                    'amount': float(payment['amount']),
                    'currency': currency.get('iso_code', ''),
                }
                payment_transactions.append(transaction_vals)
        return payment_transactions

    def parse_order(self, input_file):
        order = input_file['order']
        customer = input_file['customer']
        delivery_address = input_file['delivery_address']
        invoice_address = input_file['invoice_address']
        messages = input_file['messages']
        payment_transactions = input_file['payment_transactions']

        delivery_notes = ''
        if messages:
            delivery_notes_list = [
                msg.get('message') for msg in messages if msg.get('private') == IS_FALSE
            ]
            delivery_notes = '\n'.join(delivery_notes_list)

        order_rows = order['associations']['order_rows']['order_row']
        if not isinstance(order_rows, list):
            order_rows = [order_rows]

        presta_currency = self._client.model('currency').search_read(
            filters={
                'id': order['id_currency'],
            },
            fields=['iso_code'],
        )
        currency = presta_currency and presta_currency[0] or dict()

        carrier_tax_ids, carrier_tax_behavior = self._get_carrier_tax_ids(
            order['id_carrier'],
            delivery_address.get('id_country'),
            delivery_address.get('id_state'),
            delivery_address.get('postcode'),
        )
        wrapping_tax_ids, wrapping_tax_behavior = self._get_taxes_by_tax_rule(
            self.get_configuration('PS_GIFT_WRAPPING_TAX_RULES_GROUP'),
            delivery_address.get('id_country'),
            delivery_address.get('id_state'),
            delivery_address.get('postcode'),
        )

        parsed_order = {
            'id': order['id'],
            'ref': order['reference'],
            'current_order_state': order['current_state'],
            'date_order': order['date_add'],
            'integration_workflow_states': [order['current_state']],
            'currency': currency.get('iso_code', ''),
            'lines': [self._parse_order_row(order['id'], x) for x in order_rows],
            'payment_method': order['payment'],
            'payment_transactions': payment_transactions,
            'amount_total': (
                float(order['total_products_wt']) + float(order['total_shipping_tax_incl'])
                + float(order['total_wrapping_tax_incl']) - float(order['total_discounts_tax_incl'])
            ),
            'delivery_data': {
                'delivery_notes': delivery_notes,
                'shipping_cost_tax_excl': float(order['total_shipping_tax_excl']),
                'carrier': {'id': order['id_carrier'], 'name': ''},
                'shipping_cost': float(order['total_shipping']),
                'carrier_tax_rate': float(order['carrier_tax_rate']),
                'carrier_tax_ids': carrier_tax_ids,
                'carrier_tax_behavior': carrier_tax_behavior,  # TODO: what we have to do with that
            },
            'discount_data': {
                'total_discounts_tax_incl': float(order['total_discounts_tax_incl']),
                'total_discounts_tax_excl': float(order['total_discounts_tax_excl']),
            },
            'gift_data': {
                'do_gift_wrapping': order['gift'] == '1',
                'gift_message': order['gift_message'],
                'wrapping_tax_ids': wrapping_tax_ids,
                'total_wrapping_tax_incl': float(order['total_wrapping_tax_incl']),
                'total_wrapping_tax_excl': float(order['total_wrapping_tax_excl']),
                'wrapping_tax_behavior': wrapping_tax_behavior,  # TODO: what we have to do with ?
                'recycled_packaging': order['recyclable'] == '1',  # TODO: handle in future
            },
        }

        if customer:
            parsed_order['customer'] = self._parse_customer(customer)

        if delivery_address:
            parsed_order['shipping'] = self._parse_address(customer, delivery_address)

        if invoice_address:
            parsed_order['billing'] = self._parse_address(customer, invoice_address)

        return parsed_order

    @staticmethod
    def _parse_customer(customer):
        return {
            'id': customer['id'],
            'person_name': ' '.join([customer['firstname'], customer['lastname']]),
            'email': customer['email'],
            'language': customer['id_lang'],
            'newsletter': customer['newsletter'],
            'newsletter_date_add': customer['newsletter_date_add'],
            'customer_date_add': customer['date_add'],
        }

    @staticmethod
    def _parse_address(customer, address):
        """
        we add customer id to address id to distinguish them from each other
        """

        if not address:
            return {}

        address = {
            'id': '',
            'person_name': ' '.join([address['firstname'], address['lastname']]),
            'email': customer.get('email', ''),
            'person_id_number': address['dni'],
            'company_name': address['company'],
            'company_reg_number': address['vat_number'],
            'street': address['address1'],
            'street2': address['address2'],
            'city': address['city'],
            'country': address['id_country'],
            'state': address['id_state'] if address['id_state'] != IS_FALSE else '',
            'zip': address['postcode'],
            'phone': address['phone'],
            'mobile': address['phone_mobile'],
            'other': address['other'],
        }

        if customer.get('id_lang'):
            address['language'] = customer.get('id_lang')

        return address

    def _parse_order_row(self, order_id, row):
        filter_criteria = {
            'filter[id_order]': '[%s]' % order_id,
            'filter[product_id]': '[%s]' % row['product_id'],
            'filter[product_attribute_id]': '[%s]' % row['product_attribute_id'],
        }
        if 'id_customization' in row:
            filter_criteria['filter[id_customization]'] = row['id_customization']

        details_ids = self._client.search(
            'order_details',
            options=filter_criteria
        )
        assert len(details_ids) == 1

        taxes = []
        details = self._client.get('order_details', details_ids[0])
        tax = details['order_detail']['associations']['taxes'].get('tax')
        if tax:
            if not isinstance(tax, list):
                tax = [tax]

            taxes = [x['id'] for x in tax if x['id'] != IS_FALSE]

        return {
            'id': row['id'],
            'product_id': '%s-%s' % (row['product_id'], row['product_attribute_id']),
            'product_uom_qty': int(row['product_quantity']),
            'price_unit': float(row['unit_price_tax_excl']),
            'price_unit_tax_incl': float(row['unit_price_tax_incl']),
            'taxes': taxes,
        }

    def _get_state_code_or_empty(self, id_state):
        try:
            state = self._client.get('states', id_state)['state']
            return state['iso_code']
        except PrestaShopWebServiceError:
            return ''

    def export_inventory(self, inventory):
        for product_combination_id, inventory_item in inventory.items():
            product_id, combination_id = product_combination_id.split('-')

            # find stock for combination
            stock = self._client.model('stock_available').search({
                'id_product': product_id,
                'id_product_attribute': combination_id,
            })

            stock.quantity = int(inventory_item['qty'])
            stock.save()

    def export_tracking(self, sale_order_id, tracking_data_list):
        tracking = ', '.join(set([x['tracking'] for x in tracking_data_list]))

        order_carrier = self._client.model('order_carrier').search({
            'id_order': sale_order_id,
        })

        # TODO: check with integrational test

        order_carrier.tracking_number = tracking
        result = order_carrier.save()
        return result

    def export_sale_order_status(self, vals):
        status = vals['status']
        order_id = vals['order_id']

        order = self._client.model('order').get(order_id)
        order.current_state = status
        return order.save()

    def _import_template_custom_field_hook(self, presta_template, template_vals):
        # This method is a hook method that allows
        # to add to resulting template additional values, so then can be used later on
        # when creating product from external product
        pass

    def _import_variant_custom_field_hook(self, presta_template, presta_variant, variant_vals):
        # This method is a hook method that allows
        # to add to resulting variant additional values, so then can be used later on
        # when creating product from external product
        pass

    def _check_active_categories(self, external_category_ids):
        if external_category_ids:
            external_categories = self._client.model('category').search_read(
                filters={'id': '[%s]' % '|'.join(external_category_ids), 'active': IS_TRUE},
                fields=['id'],
            )

            external_category_ids = [x['id'] for x in external_categories]

        return external_category_ids

    def get_product_for_import(self, product_code, import_images=False):
        # Get product
        presta_template = self._client.model('product').search_read(
            filters={'id': '[%s]' % product_code},
            skip_translation=True,
        )

        if not presta_template:
            raise UserError(
                _('Product with id "%s" does not exist in PrestaShop') % product_code
            )

        if isinstance(presta_template, list):
            presta_template = presta_template[0]

        # Get combinations
        presta_variants = self._client.model('combination').search_read(
            filters={'id_product': '[%s]' % product_code},
        )

        if not isinstance(presta_variants, list):
            presta_variants = [presta_variants]

        # Fill product json
        public_category_ids = presta_template.get('associations', {})\
            .get('categories', {}).get('category', [])

        if not isinstance(public_category_ids, list):
            public_category_ids = [public_category_ids]

        # Hotfix to remove inactive categories
        public_category_ids = self._check_active_categories(
            [x['id'] for x in public_category_ids])

        external_product_category_id = presta_template['id_category_default']
        if external_product_category_id and external_product_category_id != IS_FALSE:
            if not self._check_active_categories([external_product_category_id]):
                external_product_category_id = IS_FALSE

        attribute_value_ids = presta_template.get('associations', {})\
            .get('product_option_values', {}).get('product_option_value', [])

        if not isinstance(attribute_value_ids, list):
            attribute_value_ids = [attribute_value_ids]

        product_features = presta_template.get('associations', {})\
            .get('product_features', {}).get('product_feature', [])

        if not isinstance(product_features, list):
            product_features = [product_features]

        product_features = [{
            'feature_id': x['id'],
            'feature_value_id': x['id_feature_value']
        } for x in product_features]

        accessory_ids = self._parse_accessory_ids(presta_template)

        template = {
            'name': presta_template['name'],
            'type': PRODUCT_TYPE_MAP[presta_template['type']['value']],
            'list_price': float(presta_template['price']),
            'default_code': presta_template['reference'],
            'weight': presta_template['weight'],
            'public_categ_ids': public_category_ids,
            'website_description': presta_template['description'],
            'website_short_description': presta_template['description_short'],
            'website_seo_metatitle': presta_template['meta_title'],
            'website_seo_description': presta_template['meta_description'],
            'default_public_categ_id': external_product_category_id,
            'sale_ok': presta_template['available_for_order'],
            'barcode': presta_template['ean13'],
            'active': presta_template['active'],
            'taxes_id': presta_template['id_tax_rules_group'],
            'attribute_value_ids': [x['id'] for x in attribute_value_ids],
            'product_features': product_features,
            'optional_product_ids': accessory_ids,
        }

        self._import_template_custom_field_hook(presta_template, template)

        images_hub = {
            'images': dict(),  # 'images': {'image_id': bin-data,}
            'variants': dict(),  # variants: {'variant_id': [image-ids],}
        }
        variants = []

        # Handle a variant marked as default firstly.
        for variant in sorted(presta_variants, key=lambda x: x['default_on'], reverse=True):
            attribute_value_ids = variant.get('associations', {})\
                .get('product_option_values', {}).get('product_option_value', [])

            if not isinstance(attribute_value_ids, list):
                attribute_value_ids = [attribute_value_ids]

            variant_id = f"{variant['id_product']}-{variant['id']}"

            variant_values = {
                'variant_id': variant_id,
                'standard_price': float(
                    Decimal(presta_template['wholesale_price']) +
                    Decimal(variant['wholesale_price'])
                ),
                'weight': float(
                    Decimal(presta_template['weight']) +
                    Decimal(variant['weight'])
                ),
                'barcode': variant['ean13'],
                'default_code': variant['reference'],
                'attribute_value_ids': [x['id'] for x in attribute_value_ids],
                'lst_price': float(
                    Decimal(presta_template['price']) +
                    Decimal(variant['price'])
                ),
            }

            self._import_variant_custom_field_hook(presta_template, variant, variant_values)

            variants.append(variant_values)

            image_list = variant['associations']['images'].get('image', [])

            if not isinstance(image_list, list):
                image_list = [image_list]

            if image_list:
                images_hub['variants'][variant_id] = [
                    image['id'] for image in image_list if image['id'] != IS_FALSE
                ]

        bom_components = []

        external_components = presta_template['associations']['product_bundle'].get('product', [])

        if not isinstance(external_components, list):
            external_components = [external_components]

        for bom_component in external_components:
            bom_components.append({
                'product_id': bom_component['id'],
                'variant_id': bom_component.get('id_product_attribute', IS_FALSE),
                'quantity': bom_component['quantity'],
            })

        if import_images:
            image_list_tmpl = presta_template['associations']['images'].get('image', [])

            if not isinstance(image_list_tmpl, list):
                image_list_tmpl = [image_list_tmpl]

            image_ids = [image['id'] for image in image_list_tmpl]
            bearer_url = f'{self._client._api_url}images/products/{product_code}'

            for image_id in image_ids:
                if not image_id or image_id == IS_FALSE:
                    continue

                try:
                    response = self._client._execute(f'{bearer_url}/{image_id}', 'GET')
                except PrestaShopWebServiceError:
                    pass
                else:
                    if response.status_code == 200:
                        images_hub['images'][image_id] = response.content

        return template, variants, bom_components, images_hub

    def _get_product_fields_hook(self, fields):
        # This method exists to extend amount of fields that are retrieved
        # from Prestashop API. So additional logic can be added
        fields_list = fields
        if 'id' not in fields_list:
            fields_list.append('id')
        return fields_list

    def _get_product_filter_hook(self, search_filter=None):
        # This method exists to extend filter criteria for products
        # for Prestashop API. So additional logic can be added
        kwargs = search_filter or dict()
        import_products_filter = json.loads(self.get_settings_value('import_products_filter'))
        product_filter_dict = {
            **import_products_filter,
            **kwargs,
        }
        return product_filter_dict

    def _get_combination_fields_hook(self, fields):
        # This method exists to extend amount of fields that are retrieved
        # from Prestashop API. So additional logic can be added
        fields_list = fields
        if 'id' not in fields_list:
            fields_list.append('id')
        if 'id_product' not in fields_list:
            fields_list.append('id_product')
        return fields_list

    def _get_combination_filter_hook(self, search_filter):
        # This method exists to extend filter criteria for combinations
        # for Prestashop API. So additional logic can be added
        if not isinstance(search_filter, dict):
            search_filter = {}
        return search_filter

    def _filter_templates_hook(self, templates):
        # This method is a hooks that allows to additionally filter products
        # by some non-standard logic. Designed for extension in sub-classes
        return templates

    def _get_products_and_variants(self, product_fields, combination_fields, product_filter):

        template_ids = self._client.model('product').search_read_by_blocks(
            filters=self._get_product_filter_hook(product_filter),
            fields=self._get_product_fields_hook(product_fields),
        )

        template_ids = self._filter_templates_hook(template_ids)

        active_template_ids = [x['id'] for x in template_ids]

        variant_ids = self._client.model('combination').search_read_by_blocks(
            filters=self._get_combination_filter_hook(product_filter),
            fields=self._get_combination_fields_hook(combination_fields),
        )

        # If we were searching by some criteria we have to double check now if found combinations
        # correspond to product template search criteria (usually it is {'active': 1})
        if product_filter and variant_ids:
            tmpl_ids_filter = {'id': '[%s]' % '|'.join([x['id_product'] for x in variant_ids])}
            active_templates = self._client.model('product').search_read(
                filters=self._get_product_filter_hook(tmpl_ids_filter),
                fields=self._get_product_fields_hook(['id']),
            )
            active_templates = self._filter_templates_hook(active_templates)
            # create list of ids
            active_template_ids = [x['id'] for x in active_templates]

        variant_ids = [x for x in variant_ids if x['id_product'] in active_template_ids]

        return template_ids, variant_ids

    def get_templates_and_products_for_validation_test(self, product_refs=None):
        """Presta allows different references for for template and its single variant."""
        if product_refs and not isinstance(product_refs, list):
            product_refs = [str(product_refs)]
        reference_filter_mixin = {}
        if product_refs:
            reference_filter_mixin['reference'] = '[%s]' % '|'.join(product_refs)

        def serialize_variant(var):
            return {
                'id': var['id'],
                'barcode': var['ean13'],
                'ref': var['reference'],
                'parent_id': var['id_product'],
                'skip_ref': False,
            }

        def serialize_template(tmpl):
            return {
                'id': tmpl['id'],
                'barcode': tmpl['ean13'],
                'ref': tmpl['reference'],
                'parent_id': str(),
                'skip_ref': False,
            }

        product_fields = ['id', 'ean13', 'reference']
        combination_fields = ['id', 'id_product', 'reference', 'ean13']
        template_ids, variant_ids = self._get_products_and_variants(product_fields,
                                                                    combination_fields,
                                                                    reference_filter_mixin)

        products_data = defaultdict(list)
        for tmpl in template_ids:
            products_data[tmpl['id']].append(
                serialize_template(tmpl)
            )

        for variant in variant_ids:
            products_data[variant['id_product']].append(
                serialize_variant(variant)
            )

        # If there is at least one variant, template reference is not essential.
        for product_list in products_data.values():
            if len(product_list) > 1:
                for tmpl_dict in filter(lambda x: not x['parent_id'], product_list):
                    tmpl_dict['skip_ref'] = True

        return TemplateHub(list(itertools.chain.from_iterable(products_data.values())))

    def get_products_for_accessories(self):
        _logger.info('Prestashop: get_products_for_accessories()')

        external_ids = set()
        external_data = list()
        template_router = defaultdict(set)

        templates = self._client.model('product').search_read_by_blocks(
            filters=self._get_product_filter_hook(),
        )

        for template in templates:
            accessories = self._parse_accessory_ids(template)

            if accessories:
                template_id = template['id']

                external_ids.add(template_id)
                external_ids.update(accessories)
                template_router[template_id].update(accessories)

        for template in templates:
            template_id = template['id']

            if template_id in external_ids:
                product_data = {
                    'id': template_id,
                    'name': template['name'],
                    'external_reference': template['reference'] or None,
                }
                external_data.append(product_data)
                external_ids.remove(template_id)

            if not external_ids:
                break

        return external_data, template_router

    def get_stock_levels(self):
        stock_available = self._client.model('stock_available').search_read_by_blocks(
            filters=None,
            fields=['id_product', 'id_product_attribute', 'quantity'],
        )

        stock_available = {
            x['id_product'] + '-' + x['id_product_attribute']: x['quantity']
            for x in stock_available
        }

        return stock_available

    def _parse_accessory_ids(self, template):
        accessories = template.get('associations', {}).get('accessories', {}).get('product', [])

        if isinstance(accessories, dict):
            accessories = [accessories]

        return [x['id'] for x in accessories]

    def _convert_to_html(self, id_list):
        pattern = '<li><a href="%s/sell/catalog/products/%s" target="_blank">%s</a></li>'
        arg_list = [(self.admin_url, x.split('-')[0], x) for x in id_list]
        return ''.join([pattern % args for args in arg_list])

    @staticmethod
    def _get_bad_request_webhook_message():
        url = 'https://addons.prestashop.com/en/third-party-data-integrations-crm-erp/' \
              '48921-webhooks-integration.html'
        href_to_module = f'<a href="{url}" target="_blank">' + _('Webhook Integration') + '<a/>'
        message = _(
            'By default, Prestashop does not have webhooks functionality. '
            'Webhooks can be added only via 3rd party modules. We at VentorTech '
            'investigated available solutions on the Prestashop addons market '
            'and found that this %s module is suitable for '
            'this. Also, we communicated with the developers of this module and '
            'asked them to include in their plugin a few changes that are needed '
            'for our connector. So in order to use webhooks functionality with '
            'Prestashop, you need: '
            '<br/> (1) to purchase and install specified module '
            '<br/> (2) In Prestashop admin in the menu '
            '"Advanced Parameters → Webservice" for your API Key add '
            'permissions for "webhooks" resource.'
        ) % href_to_module
        return message
