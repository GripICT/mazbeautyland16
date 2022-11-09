# See LICENSE file for full copyright and licensing details.

from ...exceptions import ApiImportError, NotMappedToExternal, NotMappedFromExternal
from ...tools import IS_FALSE
from odoo import models, fields, _
from odoo.exceptions import ValidationError
from odoo.tools.image import IMAGE_MAX_RESOLUTION
from odoo.tools.sql import escape_psql

import base64
import logging
from io import BytesIO
from collections import defaultdict

from PIL import Image

_logger = logging.getLogger(__name__)


class IntegrationProductTemplateExternal(models.Model):
    _name = 'integration.product.template.external'
    _inherit = 'integration.external.mixin'
    _description = 'Integration Product Template External'
    _odoo_model = 'product.template'

    external_product_variant_ids = fields.One2many(
        comodel_name='integration.product.product.external',
        inverse_name='external_product_template_id',
        string='External Product Variants',
        readonly=True,
    )

    def run_import_products(self, import_images=False):
        for external_template in self:
            integration = external_template.integration_id
            integration = integration.with_context(company_id=integration.company_id.id)

            integration = integration.with_delay(
                description='Import Single Product (auto-match + create Odoo product)'
            )

            integration.import_product(external_template, import_images=import_images)

        plural = ('', 'is') if len(self) == 1 else ('s', 'are')

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Import Product'),
                'message': 'Queue Job%s "Product Import" %s created' % plural,
                'type': 'success',
                'sticky': False,
            }
        }

    def import_one_product(self, ext_template, ext_products, ext_bom_components, ext_images):
        if ext_bom_components and not self.integration_id.is_installed_mrp:
            raise ValidationError(_(
                'The product %s contains bom-components,'
                'however the Manufacturing module is not installed.'
                % ext_template['name']
            ))

        existing_template = self._try_to_find_odoo_template(ext_template, ext_products)
        template = self._create_template(existing_template, ext_template, ext_images)
        # fill in missed fields
        if template.default_code and self.external_reference != template.default_code:
            self.external_reference = template.default_code
        if not self.name:
            self.name = template.name
        product_map = self._try_to_map_products(template, ext_products)
        self._update_products(ext_products, product_map, ext_images)
        self._create_boms(template, ext_bom_components)

        return template

    def _try_to_find_odoo_template(self, template, variants):
        ProductTemplate = self.env['product.template']
        tmpl_ref = template.get('default_code', False)
        tmpl_barcode = template.get('barcode', False)

        if not tmpl_ref and not variants:
            raise ApiImportError(
                _('Reference is empty for the product (%s) %s') % (self.code, self.name)
            )
        if variants:
            if not all([x.get('default_code') for x in variants]):
                raise ApiImportError(
                    _('Reference field is empty for some product variants of the product:'
                      ' (%s) %s') % (self.code, self.name)
                )
            variant_barcodes = [x.get('barcode') for x in variants]
            if any(variant_barcodes) and not all(variant_barcodes):
                raise ApiImportError(
                    _('Barcode field is empty for some product variants of the product: (%s) %s. '
                      'Note that either all variants of the same product should have barcodes, '
                      'or all variants of the same product should not have barcodes in the '
                      'external e-Commerce system') % (self.code, self.name)
                )

        # Try to find already mapped product template
        odoo_template = ProductTemplate.from_external(
            integration=self.integration_id,
            code=self.code,
            raise_error=False,
        )

        # Then let's try to find Odoo template by internal reference
        if not odoo_template and tmpl_ref:
            odoo_template = ProductTemplate.search([
                ('default_code', '=ilike', escape_psql(tmpl_ref)),
            ])

            if len(odoo_template) > 1:
                raise ApiImportError(_(
                    'There are several Odoo products with the same Internal Reference "%s".'
                ) % tmpl_ref)

        # Now let's search product by barcode (as internal references may be different, but
        # barcodes usually unique
        if not odoo_template and tmpl_barcode:
            odoo_template = ProductTemplate.search([
                ('barcode', '=like', tmpl_barcode),
            ])

            if len(odoo_template) > 1:
                raise ApiImportError(_(
                    'There are several Odoo products with the same Barcode "%s".'
                ) % tmpl_barcode)

        if odoo_template or not variants:
            return odoo_template

        # Try to find product.template by product_product.default_code or barcode
        variants_templates_dict = {}

        for ext_variant in variants:
            variant_ref = ext_variant['default_code']
            variant_barcode = ext_variant.get('barcode')

            # First search product variant by Internal Reference
            product = self._find_product_by_field('default_code',
                                                  _('Internal Reference'),
                                                  '=ilike',
                                                  escape_psql(variant_ref))

            if not product and variant_barcode:
                product = self._find_product_by_field('barcode',
                                                      _('Barcode'),
                                                      '=like',
                                                      escape_psql(variant_barcode))

            if not product:
                variants_templates_dict[variant_ref] = ProductTemplate
                continue

            odoo_template = product.product_tmpl_id
            # Remember found product template
            variants_templates_dict[variant_ref] = odoo_template

        # Now check if all variants are mapped to the same product template
        # If not - raise error as in this case auto-mapping is problematic
        error_message = _('ERROR! Variants from the same product in e-Commerce System were mapped'
                          ' to several different product templates in Odoo. Please, check below '
                          'details and fix them either on Odoo on e-Commerce System side:') + '\n'
        found_templates = set()
        for external_ref, odoo_template in variants_templates_dict.items():
            template_id = False
            if odoo_template:
                template_id = odoo_template.id
                error_message += _('Product "%s" was mapped to Odoo Template with name "%s"'
                                   ' and id=%s') % (external_ref,
                                                    odoo_template.name,
                                                    odoo_template.id)
            else:
                error_message += _('Product "%s" was not mapped to any Odoo '
                                   'Template') % external_ref
            error_message += '\n'
            found_templates.add(template_id)

        if len(found_templates) > 1:
            raise ApiImportError(error_message)

        return odoo_template

    def _find_product_by_field(self,
                               field_technical_name,
                               field_friendly_name,
                               search_criteria,
                               value):
        product = self.env['product.product'].search([
            (field_technical_name, search_criteria, value),
        ])

        if len(product) > 1:
            raise ApiImportError(_(
                'There are several product variants with the field "%s" = %s.'
            ) % (field_friendly_name, value))

        # if we have found product variant, then it is mandatory to check that all
        # it's variants are having non-empty value in the field that we are using for searching
        # as if not, we have chances that we will not be able to do auto-mapping properly
        if product:
            found_template = product.product_tmpl_id
            for variant in found_template.product_variant_ids:
                field_value = getattr(variant, field_technical_name)
                if not field_value:
                    raise ApiImportError(_('Not all product variants of the Product Template with'
                                           ' name "%s" (id=%s) has non-empty field "%s". Because '
                                           'of this it is not possible to automatically guess '
                                           'mapping. Please, fill in above '
                                           'field.') % (found_template.name,
                                                        found_template.id,
                                                        field_friendly_name)
                                         )

        return product

    def _create_template_custom_field_hook(self, odoo_template, ext_template, template_vals):
        # Method that can be extended to add custom fields during initial import procedure
        pass

    def _create_template(self, template, ext_template, ext_images):
        integration = self.integration_id
        ProductTemplate = self.env['product.template']
        ProductPublicCategory = self.env['product.public.category']
        ProductAttributeValue = self.env['product.attribute.value']
        ProductFeature = self.env['product.feature']
        ProductFeatureValue = self.env['product.feature.value']

        upd_template = {
            'name': ext_template.get('name', False),
            'type': ext_template.get('type', False),
            'list_price': ext_template.get('list_price', False),
            'weight': ext_template.get('weight', False),
            'website_description': ext_template.get('website_description', False),
            'website_short_description': ext_template.get('website_short_description', False),
            'website_seo_metatitle': ext_template.get('website_seo_metatitle', False),
            'website_seo_description': ext_template.get('website_seo_description', False),
            'sale_ok': ext_template.get('sale_ok', False),
            'active': ext_template.get('active', False),
            'integration_ids': [(4, integration.id)],
            'feature_line_ids': [(5, 0)],
        }

        self._create_template_custom_field_hook(template, ext_template, upd_template)

        # Only set internal reference for product if this is new template or
        # If default_code for template is empty
        # Because Odoo should be master data for internal references
        if not template or not template.default_code:
            upd_template['default_code'] = ext_template.get('default_code', False)

        # Find public categories in Odoo
        if (ext_template.get('default_public_categ_id')
                and ext_template['default_public_categ_id'] != IS_FALSE):
            public_categ_id = ProductPublicCategory.from_external(
                integration,
                ext_template['default_public_categ_id'],
            )
            if public_categ_id:
                upd_template['default_public_categ_id'] = public_categ_id.id

        if ext_template.get('public_categ_ids'):
            odoo_categories = ProductPublicCategory

            for ext_category_id in ext_template['public_categ_ids']:
                if ext_category_id and ext_category_id != IS_FALSE:
                    odoo_categories |= ProductPublicCategory.from_external(
                        integration,
                        ext_category_id,
                    )

            upd_template['public_categ_ids'] = [(6, 0, odoo_categories.ids)]

        if ext_template.get('optional_product_ids'):
            odoo_templates = ProductTemplate

            for ext_template_id in ext_template['optional_product_ids']:
                if ext_template_id and ext_template_id != IS_FALSE:
                    odoo_templates |= ProductTemplate.from_external(
                        integration,
                        ext_template_id,
                        False,
                    )

            upd_template['optional_product_ids'] = [(6, 0, odoo_templates.ids)]

        # Find taxes in Odoo
        tax_id = ext_template.get('taxes_id')

        if tax_id and tax_id != IS_FALSE:
            odoo_tax_id = integration.convert_external_tax_to_odoo(tax_id)

            if not odoo_tax_id:
                raise ApiImportError(_(
                    'It is not possible to import product into Odoo for "%s" integration. '
                    'External tax value "%s" may not be converted to the relevant odoo value.'
                ) % (integration.name, tax_id))

            upd_template['taxes_id'] = [(6, 0, [odoo_tax_id.id])]

        # Find product attribute values in Odoo
        attr_values_ids_by_attr_id = defaultdict(list)
        attribute_value_ids = ProductAttributeValue
        ext_attribute_value_ids = ext_template.pop('attribute_value_ids', list())

        for ext_attribute_value_id in ext_attribute_value_ids:
            if ext_attribute_value_id != IS_FALSE:
                attribute_value_ids |= ProductAttributeValue.from_external(
                    integration,
                    ext_attribute_value_id,
                )

        for attribute_value_id in attribute_value_ids:
            attribute_id = attribute_value_id.attribute_id.id
            attr_values_ids_by_attr_id[attribute_id].append(attribute_value_id.id)

        # Check barcode
        if ext_template.get('barcode') and ext_template['barcode'] != IS_FALSE:
            upd_template['barcode'] = ext_template['barcode']

        # Create images
        if ext_images['images']:
            upd_template['product_template_image_ids'] = []

            for bin_data in ext_images['images'].values():
                # verify image size
                img = Image.open(BytesIO(bin_data))
                w, h = img.size
                if w * h > IMAGE_MAX_RESOLUTION:
                    continue

                b64_image = base64.b64encode(bin_data)

                if not upd_template.get('image_1920'):
                    upd_template['image_1920'] = b64_image
                else:
                    upd_template['product_template_image_ids'].append((0, 0, {
                        'name': upd_template['name'],
                        'image_1920': b64_image,
                    }))

            if not upd_template['product_template_image_ids']:
                upd_template.pop('product_template_image_ids')

        # Create Features
        for feature_line in ext_template.get('product_features', []):
            feature = ProductFeature
            feature_value = ProductFeatureValue

            if feature_line['feature_id'] != IS_FALSE:
                feature = ProductFeature.from_external(integration, feature_line['feature_id'])

            if feature_line['feature_value_id'] != IS_FALSE:
                feature_value = ProductFeatureValue.from_external(
                    integration, feature_line['feature_value_id'])

            if feature and feature_value:
                upd_template['feature_line_ids'].append((0, 0, {
                    'feature_id': feature.id,
                    'feature_value_id': feature_value.id,
                }))

        template = template.with_context(skip_product_export=True)

        if not template:
            attribute_line_ids = [
                (0, 0, {
                    'attribute_id': attr_id,
                    'value_ids': [(6, 0, value_ids)],
                }) for attr_id, value_ids in attr_values_ids_by_attr_id.items()
            ]
            if attribute_line_ids:
                upd_template['attribute_line_ids'] = attribute_line_ids

            template = self.create_or_update_with_translation(
                integration=self.integration_id,
                odoo_object=template,
                vals=upd_template,
            )
        else:
            # remove existing product image's
            if upd_template.get('product_template_image_ids'):
                template.product_template_image_ids.unlink()

            template = self.create_or_update_with_translation(
                integration=self.integration_id,
                odoo_object=template,
                vals=upd_template,
            )

            # append/create attribute values to template
            for attr_id, value_ids in attr_values_ids_by_attr_id.items():
                existing_line = template.attribute_line_ids.filtered(
                    lambda line: line.attribute_id.id == attr_id
                )
                if existing_line:
                    existing_line.value_ids = [(6, 0, value_ids)]
                else:
                    template.attribute_line_ids = [(0, 0, {
                        'attribute_id': attr_id,
                        'value_ids': [(6, 0, value_ids)],
                    })]

        template.create_or_update_mapping(integration, template, self)

        return template

    def _try_to_map_products(self, template, ext_products):
        """
        :return: {'49-174': product.product(393,), '49-175': product.product(394,)}
        """
        integration = self.integration_id
        ProductProduct = self.env['product.product']
        ProductProductExternal = self.env['integration.product.product.external']
        ProductProductMapping = self.env['integration.product.product.mapping']

        external_products = ProductProductExternal.search([
            ('integration_id', '=', integration.id),
            ('code', '=like', self.code + '-%'),
        ])
        default_ext_code = self.code + '-0'
        external_codes = [x['variant_id'] for x in ext_products] or [default_ext_code]
        external_reference_by_code = {
            x['variant_id']: x['default_code'] for x in ext_products
        }
        external_names_by_code = {
            x['variant_id']: x.get('name', self.name) for x in ext_products
        }
        external_reference_by_code[default_ext_code] = self.external_reference
        external_names_by_code[default_ext_code] = self.name

        # Delete unnecessary ProductProductExternal
        to_unlink = external_products.filtered(lambda x: x.code not in external_codes)
        existing_external_codes = {
            x.code: x for x in (external_products - to_unlink)
        }
        to_unlink.unlink()

        # Create new ProductProductExternal
        for code in filter(lambda x: x not in existing_external_codes, external_codes):
            external = ProductProductExternal.create({
                'integration_id': integration.id,
                'code': code,
                'name': external_names_by_code.get(code, False),
                'external_reference': external_reference_by_code.get(code, False),
                'external_product_template_id': self.id,
            })
            existing_external_codes[code] = external

        product_ids = template and template.product_variant_ids or ProductProduct

        # A: single variant
        if len(ext_products) <= 1:
            code = external_codes[0] if ext_products else default_ext_code
            external = existing_external_codes[code]

            assert len(product_ids) == 1 or not template

            ProductProduct.create_or_update_mapping(integration, product_ids, external)
            return {
                external.code: product_ids,
            }

        # B: multiple variants
        if product_ids:
            product_mappings = ProductProductMapping.search([
                ('integration_id', '=', integration.id),
                ('product_id', 'in', product_ids.ids),
            ])

        code_product = dict()
        # Find existing variants and mapping it
        for product_id in product_ids:
            external = product_mappings\
                .filtered(lambda x: x.product_id == product_id).external_product_id

            # Try to find by default_code
            if not external and product_id.default_code:
                for variant in ext_products:
                    if variant['default_code'] == product_id.default_code:
                        external = existing_external_codes[variant['variant_id']]
                        break

            # Try to find by barcode
            if not external and product_id.barcode:
                for variant in ext_products:
                    if variant.get('barcode') and variant['barcode'] != IS_FALSE \
                            and variant['barcode'] == product_id.barcode:
                        external = existing_external_codes[variant['variant_id']]
                        break

            # Try to find by product_attribute_value
            if not external:
                attribute_value_ids = set()

                for attribute_value_id in product_id.product_template_attribute_value_ids:
                    code = attribute_value_id.product_attribute_value_id\
                        .to_external(integration)

                    attribute_value_ids.add(code)

                for variant in ext_products:
                    if set(variant['attribute_value_ids']) == attribute_value_ids:
                        if variant['variant_id'] not in code_product:
                            external = existing_external_codes[variant['variant_id']]
                            break

            if external:
                ProductProduct.create_or_update_mapping(integration, product_id, external)
                code_product[external.code] = product_id

        # Create empty mappings
        for external in existing_external_codes.values():
            if not ProductProductMapping.search([
                ('integration_id', '=', integration.id),
                ('external_product_id', '=', external.id),
            ]):
                ProductProduct.create_or_update_mapping(integration, None, external)

        return code_product

    def _update_variant_custom_field_hook(self, odoo_variant, ext_variant, variant_vals):
        # Method that can be extended to add custom fields during initial import procedure
        pass

    def _update_products(self, ext_products, product_map, ext_images):
        for ext_product in ext_products:
            variant_id = ext_product['variant_id']
            product = product_map.get(variant_id, False)
            image_list = ext_images['variants'].get(variant_id, [])

            if not product:
                raise ApiImportError(
                    _('Can\'t find product variant with code %s in Odoo') % variant_id
                )

            upd_product = {
                'standard_price': ext_product.get('standard_price', False),
                'weight': ext_product.get('weight', False),
                'integration_ids': [(4, self.integration_id.id)],
            }

            # Do not update internal reference in case it exists
            # Because Odoo as WMS system should be the main system for internal references data
            if not product.default_code:
                upd_product['default_code'] = ext_product['default_code']

            if ext_product.get('lst_price'):
                upd_product['variant_extra_price'] = (
                    ext_product['lst_price'] - (product.lst_price - product.variant_extra_price)
                )

            if ext_product.get('barcode') and ext_product['barcode'] != IS_FALSE:
                upd_product['barcode'] = ext_product['barcode']

            if image_list and ext_images['images']:
                img_data = ext_images['images'][image_list[0]]
                upd_product['image_1920'] = base64.b64encode(img_data)

            self._update_variant_custom_field_hook(product, ext_product, upd_product)
            product.with_context(skip_product_export=True).write(upd_product)

    def _create_boms(self, template, ext_bom_components):
        integration = self.integration_id
        ProductTemplate = self.env['product.template']
        ProductProduct = self.env['product.product']
        ProductTemplateExternal = self.env['integration.product.template.external']

        # Check BOM components
        for ext_bom_component in ext_bom_components:
            bom_product = ProductTemplate.from_external(
                integration,
                ext_bom_component['product_id'],
                raise_error=False,
            )

            if not bom_product:
                product_external = ProductTemplateExternal.get_external_by_code(
                    integration,
                    ext_bom_component['product_id'],
                    raise_error=False,
                )

                if not product_external:
                    raise ApiImportError(
                        _('Can not find BOM component %s ') % ext_bom_component['product_id']
                    )

                bom_product = integration.import_product(product_external, True)

            bom_variant = None

            if ext_bom_component['variant_id'] != IS_FALSE:
                bom_variant = ProductProduct.from_external(
                    integration,
                    ext_bom_component['product_id'] + '-' + ext_bom_component['variant_id'],
                )
            else:
                bom_variant = bom_product.product_variant_ids[0]

            ext_bom_component['product_id'] = bom_product.id
            ext_bom_component['variant_id'] = bom_variant.id

        if ext_bom_components:
            template.bom_ids.unlink()

            bom_line_ids = []
            for component in ext_bom_components:
                bom_line_ids.append((0, 0, {
                    'product_id': component['variant_id'],
                    'product_qty': component['quantity'],
                }))

            self.env['mrp.bom'].with_context(skip_product_export=True).create({
                'product_tmpl_id': template.id,
                'product_qty': 1,
                'bom_line_ids': bom_line_ids,
                'type': 'phantom',
            })

    def try_map_template_and_variants(self, ext_template):
        self.ensure_one()
        ext_products = [
            {
                'default_code': x.get('external_reference'),
                'barcode': x.get('barcode'),
                'variant_id': x.get('id'),
                'attribute_value_ids': x.get('attribute_value_ids'),
            }
            for x in ext_template.get('variants', [])
        ]

        ext_template = {
            'default_code': ext_template['external_reference'],
            'barcode': ext_template['barcode'],
        }

        ProductTemplate = self.env['product.template']

        odoo_template = ProductTemplate.from_external(
            self.integration_id,
            self.code,
            raise_error=False
        )

        try:
            if not odoo_template:
                odoo_template = self._try_to_find_odoo_template(ext_template, ext_products)

                ProductTemplate.create_or_update_mapping(
                    self.integration_id, odoo_template, self)

            self._try_to_map_products(odoo_template, ext_products)
        except (ApiImportError, NotMappedToExternal, NotMappedFromExternal):
            pass
