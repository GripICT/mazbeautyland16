# See LICENSE file for full copyright and licensing details.

from ..tools import _guess_mimetype
from ..models.sale_integration import EXPORT_EXTERNAL_BLOCK
from .template_converter import TemplateConverter
from odoo.exceptions import ValidationError, UserError
from odoo import models, fields, api, _

import logging

from lxml import etree


_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _name = 'product.template'
    _inherit = ['product.template', 'integration.model.mixin']
    _description = 'Product Template'
    _internal_reference_field = 'default_code'

    default_public_categ_id = fields.Many2one(
        comodel_name='product.public.category',
        string='Default Category',
    )

    public_categ_ids = fields.Many2many(
        comodel_name='product.public.category',
        relation='product_public_category_product_template_rel',
        string='Website Product Category',
    )

    public_filter_categ_ids = fields.Many2many(
        comodel_name='product.public.category',
        compute='_compute_public_filter_categories',
        string='Website Product Category Filter',
    )

    product_template_image_ids = fields.One2many(
        comodel_name='product.image',
        inverse_name='product_tmpl_id',
        string='Extra Product Media',
        copy=True,
    )

    website_product_name = fields.Char(
        string='Product Name',
        translate=True,
        help='Sometimes it is required to define separate field with beautiful product name. '
             'And standard field to use for technical name in Odoo WMS (usable for Warehouses). '
             'If current field is not empty it will be used for sending to '
             'e-Commerce System instead of standard field.'
    )

    website_description = fields.Html(
        string='Website Description',
        sanitize=False,
        translate=True,
    )

    website_short_description = fields.Html(
        string='Short Description',
        sanitize=False,
        translate=True,
    )

    website_seo_metatitle = fields.Char(
        string='Meta title',
        translate=True,
    )

    website_seo_description = fields.Char(
        string='Meta description',
        translate=True,
    )

    feature_line_ids = fields.One2many(
        comodel_name='product.template.feature.line',
        string='Product Features',
        inverse_name='product_tmpl_id',
    )

    optional_product_ids = fields.Many2many(
        'product.template', 'product_optional_rel', 'src_id', 'dest_id',
        string='Optional Products', check_company=True)

    def _search_integrations(self, operator, value):
        if operator not in ('in', '!=', '='):
            return []

        search_value = value
        # Allow setting non-realistic value just to allow adding additional
        # search criteria
        if type(value) is int and value < 0 and operator in ('!=', '='):
            search_value = False
        variants = self.env['product.product'].search([
            ('integration_ids', operator, search_value),
        ])

        template_ids = variants.mapped('product_tmpl_id').ids
        # This is a trick for the search criteria when we want to find product templates
        # where ALL variants do not have ANY integration set ('integration_ids', '=', False)
        # OR find product templates where ALL variants have some integrations set
        # ('integration_ids', '!=', False)
        # OR find all products where some products are without integrations and some with
        # ('integration_ids', '=', -1)
        if search_value is False and operator in ('!=', '='):
            alternative_operator = '='
            if '=' == operator:
                alternative_operator = '!='
            alt_template_ids = self.env['product.product'].search([
                ('integration_ids', alternative_operator, search_value),
            ]).mapped('product_tmpl_id').ids
            if type(value) is int and value < 0:
                # This is special case to have intersections between 2 sets
                # So we find templates that both have variants with and without integrations
                template_ids = list(set(template_ids) & set(alt_template_ids))
            else:
                # Now we need to find difference between found templates
                # And templates that our found with opposite criteria
                template_ids = list(set(template_ids) - set(alt_template_ids))

        return [('id', 'in', template_ids)]

    integration_ids = fields.Many2many(
        comodel_name='sale.integration',
        relation='sale_integration_product',
        column1='product_id',
        column2='sale_integration_id',
        compute='_compute_integration_ids',
        inverse='_set_integration_ids',
        domain=[('state', '=', 'active')],
        search=_search_integrations,
        string='Sales Integrations',
        default=lambda self: self._prepare_default_integration_ids(),
        help='Allow to select which channel this product should be synchronized to. '
             'By default it syncs to all.',
    )

    @api.depends('product_variant_ids', 'product_variant_ids.integration_ids')
    def _compute_integration_ids(self):
        for template in self:
            integration_ids = []

            if len(template.product_variant_ids) == 1:
                integration_ids = template.product_variant_ids.integration_ids.ids

            template.integration_ids = [(6, 0, integration_ids)]

    def _set_integration_ids(self):
        for template in self:
            if len(template.product_variant_ids) == 1:
                integration_ids = template.integration_ids.ids
                template.product_variant_ids.integration_ids = [(6, 0, integration_ids)]

    @api.depends('public_categ_ids')
    def _compute_public_filter_categories(self):
        for rec in self:
            category_ids = list()
            rec_categories = rec.public_categ_ids

            if not rec_categories:
                category_ids = rec_categories.search([]).ids
            else:
                for category in rec_categories:
                    category_ids.extend(
                        category.parse_parent_recursively()
                    )

            rec.public_filter_categ_ids = [(6, 0, category_ids)]

    @api.model
    def create(self, vals_list):
        # We need to avoid calling export separately
        # from product.template and product.product
        ctx = dict(self.env.context, from_product_template=True, from_product_create=True)
        from_product_product = ctx.pop('from_product_product', False)

        template = super(ProductTemplate, self.with_context(ctx)).create(vals_list)
        if not from_product_product:
            # If template has multiple variants, then we need to set integrations
            # On all products after product template is saved and all variants are created
            if 'integration_ids' in vals_list:
                if len(template.product_variant_ids) > 1:
                    template.product_variant_ids.integration_ids = vals_list['integration_ids']

            export_images = self._need_export_images(vals_list)
            template.trigger_export(export_images=export_images)

        return template

    def write(self, vals):
        # We need to avoid calling export separately
        # from product.template and product.product
        ctx = dict(self.env.context, from_product_template=True)
        from_product_product = ctx.pop('from_product_product', False)

        result = super(ProductTemplate, self.with_context(ctx)).write(vals)

        if not from_product_product:
            export_images = self._need_export_images(vals)
            self.trigger_export(export_images=export_images)

        return result

    @api.model
    def fields_view_get(self, view_id=None, view_type='form', toolbar=False, submenu=False):
        form_data = super().fields_view_get(
            view_id=view_id,
            view_type=view_type,
            toolbar=toolbar,
            submenu=submenu,
        )

        if view_type == 'search':
            form_data = self._update_template_form_architecture(form_data)

        return form_data

    @api.onchange('public_categ_ids')
    def _onchange_public_categ_ids(self):
        category_ids = list()

        for category in self.public_categ_ids:
            category_ids.extend(
                category._origin.parse_parent_recursively()
            )

        category_id = self.default_public_categ_id.id
        if category_id and category_ids and category_id not in category_ids:
            self.default_public_categ_id = False

    def change_external_integration_template(self):
        message_pattern = self._get_change_external_message()
        active_ids = self.env.context.get('active_ids')
        active_model = self.env.context.get('active_model')
        message = message_pattern % len(active_ids)

        if active_model == self._name:  # Convert templates to variants
            variants = self.browse(active_ids).mapped('product_variant_ids')

            active_ids = variants.ids
            active_model = variants._name

        context = {
            'active_ids': active_ids,
            'active_model': active_model,
            'default_message': message,
        }

        return {
            'name': _('Change External Integration'),
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': 'external.integration.wizard',
            'target': 'new',
            'context': context,
        }

    @staticmethod
    def _get_change_external_message():
        return _(
            'Totally %s products are selected. You can define if selected products will'
            'be synchronised to specific sales integration. Sales Integrations only in "Active"'
            'state are displayed below. Note that you can define this also on'
            '"e-Commerce Integration" tab of every product/product variant individually.'
        )

    @api.model
    def _need_export_images(self, vals_list):
        return self._check_fields_changed(
            [
                'image_1920',
                'product_template_image_ids',
            ],
            vals_list
        )

    def export_images_to_integration(self):
        for integration in self.integration_ids:
            integration.export_images(self)

    def trigger_export(self, export_images=False, force_integration=None):
        # We need to allow skipping exporting in some cases
        # hence adding context here in generic action
        if self.env.context.get('skip_product_export'):
            return

        # If we are creating a new product than we always export images
        # We need this because when new product is created, then system is calling create()
        # and write() methods on product template. And trigger_export() is called twice with
        # export_images=False and export_images=True. That causing duplicated job to be created
        # As result duplicated products are created on e-Commerce system!
        if self.env.context.get('from_product_create'):
            export_images = True

        templates = self

        # If 'manual_trigger' is set, then we need to not check 'export_template_job_enabled'.
        manual_trigger = self.env.context.get('manual_trigger')

        # if len(self) less then EXPORT_EXTERNAL_BLOCK we shouldn't create cron for block
        use_jobs_for_blocks = len(self) > EXPORT_EXTERNAL_BLOCK

        while templates:
            templates_block = templates[:EXPORT_EXTERNAL_BLOCK]

            if use_jobs_for_blocks:
                templates_block = templates_block.with_delay(
                    description='Export Template. Prepare Templates')

            templates_block.trigger_export_by_block(
                export_images, force_integration, manual_trigger)

            templates = templates[EXPORT_EXTERNAL_BLOCK:]

    def trigger_export_by_block(self, export_images, force_integration, manual_trigger):
        for template in self:
            name_job = '' if manual_trigger else 'export_template'
            integrations = self.env['sale.integration']\
                .get_integrations(name_job, template.company_id)

            if force_integration and force_integration in integrations:
                integrations = force_integration

            variant_integrations = template.product_variant_ids.mapped('integration_ids')
            required_integrations = integrations.filtered(lambda x: x in variant_integrations)

            for integration in required_integrations:
                # identity_key contains export_images flag because we want to be sure
                # that we didn't skip exporting images if there is job with export_images=False
                key = f'export_template_{integration.id}_{template.id}_{export_images}'
                integration = integration.with_context(company_id=integration.company_id.id)

                # In case we clicked button to export product manually, we need to raise error
                # So user will know that there are mistakes
                # Otherwise we skip validation at this stage as it will be called
                # in Export Template job later
                if manual_trigger:
                    template.validate_in_odoo(integration)

                delayable = integration.with_delay(
                    identity_key=key,
                    description='Export Template. Export Single Template',
                )
                if not integration.allow_export_images:
                    export_images = False
                delayable.export_template(template, export_images=export_images)

    def validate_in_odoo(self, integration):
        # Before export double check that all product variants
        # are having internal reference set
        variants = self.product_variant_ids.filtered(
            lambda x: integration.id in x.integration_ids.ids
        )
        if not all([x.default_code for x in variants]):
            raise UserError(
                _('Product Template "%s" (or some of it\'s variants) '
                  'do not have Internal Reference defined. '
                  'This field is mandatory for the integration as it is '
                  'used for automatic mapping') % self.name
            )

        # We also should check if product do not have duplicated internal reference
        # As in Odoo standard duplicated reference is allowed
        # But we do not want to have it in external e-Commerce System
        internal_references = variants.mapped('default_code')
        grouped_products = self.env['product.product'].read_group(
            [('default_code', 'in', internal_references)],
            ['default_code'],
            ['default_code'],
        )
        dup_refs = [x['default_code'] for x in grouped_products if x['default_code_count'] > 1]
        if len(dup_refs) > 0:
            raise UserError(
                _('Multiple products found with the same '
                  'internal reference(s): %s') % ', '.join(dup_refs)
            )

    def to_export_format(self, integration):
        self.ensure_one()
        contexted_template = self.with_context(active_test=False)
        return TemplateConverter(integration).convert(contexted_template)

    def to_images_export_format(self, integration):
        self.ensure_one()

        template_images_data = self._template_or_variant_to_images_export_format(
            self,
            integration,
        )

        products_images_data = []
        for product in self.product_variant_ids:
            image_data = self._template_or_variant_to_images_export_format(
                product,
                integration,
            )
            products_images_data.append(image_data)

        result = {
            'template': template_images_data,
            'products': products_images_data,
        }
        return result

    @api.model
    def _template_or_variant_to_images_export_format(self, record, integration):
        if record._name == 'product.template':
            extra_images = record.product_template_image_ids
            default_image_field = 'image_1920'
        else:
            extra_images = record.product_variant_image_ids
            default_image_field = 'image_variant_1920'

        default_image_data = record[default_image_field]
        if default_image_data:
            default_image = {
                'data': default_image_data,
                'mimetype': _guess_mimetype(default_image_data),
            }
        else:
            default_image = None

        extra_images_data = []
        for extra_image in extra_images:
            extra_image_data = {
                'data': extra_image.image_1920,
                'mimetype': _guess_mimetype(extra_image.image_1920)
            }
            extra_images_data.append(extra_image_data)

        external_record = record.to_external_record(integration)

        images_data = {
            'id': external_record.code,
            'default': default_image,
            'extra': extra_images_data,
            'default_code': external_record.external_reference,
        }

        return images_data

    def _template_ecommerce_field_domain(self, integration, external_code):
        search_domain = [
            ('integration_id', '=', integration.id),
            ('odoo_model_id', '=', self.env.ref('product.model_product_template').id),
        ]
        if external_code:
            search_domain.append(('send_on_update', '=', True))

        return search_domain

    def _template_converter_update(self, template_data, integration, external_record):
        """Hook method for redefining."""
        return template_data

    def _update_template_form_architecture(self, form_data):
        active_integrations = self.get_active_integrations()

        if not active_integrations:
            return form_data

        arch_tree = etree.fromstring(form_data['arch'])

        for integration in active_integrations:
            arch_tree.append(etree.Element('filter', attrib={
                'string': integration.name.capitalize(),
                'name': f'filter_{integration.type_api}_{integration.id}',
                'domain': f'[("integration_ids", "=", {integration.id})]',
            }))

        form_data['arch'] = etree.tostring(arch_tree, encoding='unicode')

        return form_data

    # -------- Converter Specific Methods ---------
    def get_integration_name(self, integration):
        self.ensure_one()
        name_field = 'name'
        if self.website_product_name:
            name_field = 'website_product_name'
        return integration.convert_translated_field_to_integration_format(
            self, name_field
        )

    def get_default_category(self, integration):
        self.ensure_one()
        default_category = self.default_public_categ_id
        if default_category:
            return default_category.to_external_or_export(integration)
        else:
            return None

    def get_categories(self, integration):
        return [
            x.to_external_or_export(integration)
            for x in self.public_categ_ids
        ]

    def get_taxes(self, integration):
        result = []
        integration_company_taxes = self.taxes_id.filtered(
            lambda x: x.company_id == integration.company_id
        )
        for tax in integration_company_taxes:
            external_tax = tax.to_external_record(integration)

            external_tax_group = self.env['integration.account.tax.group.external'].search([
                ('integration_id', '=', integration.id),
                ('external_tax_ids', '=', external_tax.id),
            ], limit=1)

            if not external_tax_group:
                raise ValidationError(_(
                    'It is not possible export product to e-Commerce System, because you '
                    'haven\'t defined Tax Group for External Tax "%s". Please,  click '
                    '"Quick Configuration" button on your integration "%s" to define that mapping.'
                ) % (external_tax.code, integration.name))

            result.append({
                'tax_id': external_tax.code,
                'tax_group_id': external_tax_group.code,
            })

        return result

    def get_product_features(self, integration):
        return [
            {
                'id': feature_line.feature_id.to_external_or_export(integration),
                'id_feature_value': feature_line.feature_value_id.to_external_or_export(integration)
            }
            for feature_line in self.feature_line_ids
        ]
