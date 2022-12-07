# See LICENSE file for full copyright and licensing details.

import json
import logging
import pytz
import traceback
from io import StringIO
from datetime import datetime, timedelta

from cerberus import Validator

from ..tools import raise_requeue_job_on_concurrent_update
from odoo.tools import config
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from psycopg2 import OperationalError
import odoo.release as release

from ..api.no_api import NoAPIClient
from ..tools import HtmlWrapper


DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'


MAPPING_EXCEPT_LIST = [
    'integration.account.tax.group.mapping',
]
LOG_SEPARATOR = '================================'
IMPORT_EXTERNAL_BLOCK = 500  # Don't make more, because of 414 Request-URI Too Large error
EXPORT_EXTERNAL_BLOCK = 500

DEFAULT_LOG_LABEL = 'Sale Integration Webhook'
PRODUCT_QTY_FIELDS = ['free_qty', 'qty_available', 'virtual_available']

_logger = logging.getLogger(__name__)


class SaleIntegration(models.Model):
    _name = 'sale.integration'
    _description = 'Sale Integration'

    name = fields.Char(
        string='Name',
        required=True,
    )
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
    )
    type_api = fields.Selection(
        selection=[('no_api', 'Not Use API')],
        string='Api service',
        required=True,
        ondelete={
            'no_api': 'cascade',
        },
    )
    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('active', 'Active'),
        ],
        string='Status',
        required=True,
        default='draft',
    )
    field_ids = fields.One2many(
        comodel_name='sale.integration.api.field',
        inverse_name='sia_id',
        string='Fields',
    )
    test_method = fields.Selection(
        '_get_test_method',
        string='Test Method',
    )
    location_ids = fields.Many2many(
        comodel_name='stock.location',
        string='Locations',
        domain=[
            ('usage', '=', 'internal'),
        ],
    )
    last_receive_orders_datetime = fields.Datetime(
        default=fields.Datetime.now,
    )
    receive_orders_cron_id = fields.Many2one(
        comodel_name='ir.cron',
    )
    import_payments = fields.Boolean('Import Payments')

    export_template_job_enabled = fields.Boolean(
        string='Export Product Template Job Enabled',
        default=False,
    )
    export_inventory_job_enabled = fields.Boolean(
        string='Send inventory in real-time',
        default=False,
    )
    synchronize_all_inventory_periodically = fields.Boolean(
        string='Synchronize all inventory periodically',
        default=False,
    )
    inventory_synchronization_cron_id = fields.Many2one(
        comodel_name='ir.cron',
    )
    next_inventory_synchronization_date = fields.Datetime(
        string='Next Synchronization Date',
        compute='_compute_next_inventory_synchronization_date',
        readonly=False,
        inverse='_inverse_next_inventory_synchronization_date',
    )
    export_tracking_job_enabled = fields.Boolean(
        default=False,
    )
    export_sale_order_status_job_enabled = fields.Boolean(
        default=False,
    )
    product_ids = fields.Many2many(
        'product.template', 'sale_integration_product', 'sale_integration_id', 'product_id',
        'Products',
        copy=False,
        check_company=True,
    )

    discount_product_id = fields.Many2one(
        comodel_name='product.product',
        string='Discount Product',
        domain="[('type', '=', 'service')]",
    )

    positive_price_difference_product_id = fields.Many2one(
        comodel_name='product.product',
        string='Price Difference Product (positive)',
        domain="[('type', '=', 'service')]",
    )

    negative_price_difference_product_id = fields.Many2one(
        comodel_name='product.product',
        string='Price Difference Product (negative)',
        domain="[('type', '=', 'service')]",
    )

    gift_wrapping_product_id = fields.Many2one(
        comodel_name='product.product',
        string='Gift Wrapping Product',
    )

    is_installed_mrp = fields.Boolean(
        compute='_compute_is_installed_mrp',
        string='Is MRP Installed',
    )

    run_action_on_cancel_so = fields.Boolean(
        string='Run Action on Cancel SO',
        copy=False,
        help='Select if you would like run action on cancel SO.',
    )

    sub_status_cancel_id = fields.Many2one(
        comodel_name='sale.order.sub.status',
        string='Cancelled Orders Sub-Status',
        domain='[("integration_id", "=", id)]',
        copy=False,
        help='Sub-status that can be set after cancelled SO',
    )

    run_action_on_shipping_so = fields.Boolean(
        string='Run Action on Shipping SO',
        copy=False,
        help='Select if you would like run action on shipping SO.',
    )

    sub_status_shipped_id = fields.Many2one(
        comodel_name='sale.order.sub.status',
        string='Shipped Orders Sub-Status',
        domain='[("integration_id", "=", id)]',
        copy=False,
        help='Sub-status that can be set after shipped SO',
    )

    run_action_on_so_invoice_status = fields.Boolean(
        string='Run Action on Invoiced or Paid SO',
        copy=False,
        help='Select if you would like run action on invoiced or paid SO. '
        'Note that you can configure specific behavior depending on payment method in menu '
        '"Auto-Workflow -> Payment Methods". See the column "Send payment status when". '
        'By default action will be executed when order is fully invoiced and all related invoices '
        'are marked as "Paid" ("In Payment")',
    )

    sub_status_paid_id = fields.Many2one(
        comodel_name='sale.order.sub.status',
        string='Paid Orders Sub-Status',
        domain='[("integration_id", "=", id)]',
        copy=False,
        help='Sub-status that can be set after paid SO',
    )

    is_configuration_wizard_exists = fields.Boolean(
        compute='_compute_configuration_wizard_exists',
    )

    apply_to_products = fields.Boolean(
        string='Add new products automatically',
        default=True,
        help=(
            'Select if you would like to add automatically all new products to this '
            'integration. So they will be exported from Odoo and appear in your e-Commerce system.'
        )
    )

    auto_create_taxes_on_so = fields.Boolean(
        string='Auto-create taxes in Odoo on SO import',
        default=False,
        help=(
            'When sales order is downloaded and tax mapping is not found, '
            'automatically create tax in Odoo. If not set, Sales Order creation will be failing '
            'and admin will have to add tax mapping manually.'
        )
    )

    auto_create_delivery_carrier_on_so = fields.Boolean(
        string='Auto-create delivery carrier in Odoo on SO import',
        default=False,
        help=(
            'When sales order is downloaded and delivery carrier is not found, '
            'automatically create delivery carrier in Odoo. If not set, Sales Order creation '
            'will be failing and admin will have to add tax mapping manually.'
        )
    )

    webhook_line_ids = fields.One2many(
        comodel_name='integration.webhook.line',
        inverse_name='integration_id',
        string='Webhook Lines',
        help="""WEBHOOK STATES:

            - Green: active webhook.
            - Red: inactive webhook.
            - Yellow: webhook need to be recreated, click button "Create Webhooks" above.
        """,
    )

    save_webhook_log = fields.Boolean(
        string='Save Logs',
    )

    allow_export_images = fields.Boolean(
        string='Allow export images',
        default=True,
    )

    synchronise_qty_field = fields.Selection(
        selection='_calc_qty_fields_selections',
        string='Synchronise quantity from the field',
        required=True,
        default='free_qty'
    )

    send_inactive_product = fields.Boolean(
        string='Send product as “inactive“ on initial export',
        help='In some cases in Odoo, you only create basic product information. And in the '
             'external system, you start adding a beautiful description. By enabling this '
             'setting product will be sent as “inactive“ on initial export, so you can '
             'review it and publish it later.',
    )

    request_order_url = fields.Char(
        string='Request Order URL',
        compute='_compute_request_order_url',
        help='URL to send requests to this integration.'
             'You can use it in your external system to send requests to Odoo.'
             '<order_id> - must be replaced with order ID in external system.'
             '<integration_api_key> - must be replaced with integration API key.',
    )

    def _compute_request_order_url(self):
        host = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        db_name = self.env.cr.dbname
        ResConfig = self.env['res.config.settings']
        integration_api_key = ResConfig.get_integration_api_key() or 'integration_api_key=<...>'

        for integration in self:
            url = f'{host}/{db_name}/integration/{self.id}/external-order/' \
                  f'<order_id>?integration_api_key={integration_api_key}'
            integration.request_order_url = url

    def _compute_next_inventory_synchronization_date(self):
        for integration in self.filtered('inventory_synchronization_cron_id'):
            cron = integration.inventory_synchronization_cron_id.sudo()
            integration.next_inventory_synchronization_date = cron.nextcall

    def _inverse_next_inventory_synchronization_date(self):
        for integration in self.filtered('inventory_synchronization_cron_id'):
            cron = integration.inventory_synchronization_cron_id.sudo()
            cron.nextcall = integration.next_inventory_synchronization_date

    @api.model
    def _calc_qty_fields_selections(self):
        return [
            (field_name, self.env['product.product']._fields[field_name].string)
            for field_name in PRODUCT_QTY_FIELDS
        ]

    def is_carrier_tracking_required(self):
        """
        Value for filtering `done order pickings`
        during `order_export_tracking()` method performing.
        """
        return False

    def open_mrp_module(self):
        """Open the standard form-view of the `Manufacturing` module."""
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ir.module.module',
            'name': 'Manufacturing',
            'view_mode': 'form',
            'view_id': self.env.ref('base.module_form').id,
            'res_id': self.env.ref('base.module_mrp').id,
            'target': 'current',
        }

    def open_webhooks_logs(self):
        tree = self.env.ref('base.ir_logging_tree_view')
        form = self.env.ref('base.ir_logging_form_view')

        integration_log = self.env['ir.logging'].search([
            ('type', '=', 'client'),
            ('line', '=', self.name),
            ('name', '=', DEFAULT_LOG_LABEL),
        ])

        return {
            'type': 'ir.actions.act_window',
            'name': 'Integration Webhook Logs',
            'res_model': 'ir.logging',
            'view_mode': 'tree,form',
            'view_ids': [
                (0, 0, {'view_mode': 'tree', 'view_id': tree.id}),
                (0, 0, {'view_mode': 'form', 'view_id': form.id}),
            ],
            'domain': [('id', 'in', integration_log.ids)],
            'target': 'current',
        }

    def create_webhooks(self, raise_original=False):
        self.ensure_one()
        routes_dict = self.prepare_webhook_routes()
        external_ids = self.webhook_line_ids.mapped('external_ref')

        try:
            adapter = self._build_adapter()
            adapter.unlink_existing_webhooks(external_ids)
            data_dict = adapter.create_webhooks_from_routes(routes_dict)
        except Exception as ex:
            if raise_original:
                raise ex

            if self.is_prestashop() and (ex.args[0] == 'Method Not Allowed'
                                         or ex.args[0] == 'Unauthorized'):
                message = adapter._get_bad_request_webhook_message()
                message_wizard = self.env['message.wizard'].create({
                    'message': '',
                    'message_html': message,
                })
                return message_wizard.run_wizard('integration_message_wizard_html_form')

            raise ValidationError(ex) from ex

        lines = self.create_integration_webhook_lines(data_dict)
        return lines

    def drop_webhooks(self):
        result = False
        external_ids = self.webhook_line_ids.mapped('external_ref')

        try:
            adapter = self._build_adapter()
            result = adapter.unlink_existing_webhooks(external_ids)
        except Exception as ex:
            result = ex.args[0]
            _logger.error(ex)
        finally:
            self.webhook_line_ids.unlink()

        return result

    def create_integration_webhook_lines(self, data_dict):
        vals_list = list()
        default_vals = {
            'integration_id': self.id,
            'original_base_url': self._get_base_url_or_debug(),
        }

        for (controller_method, name, technical_name), reference in data_dict.items():
            vals = {
                'name': name,
                'technical_name': technical_name,
                'controller_method': controller_method,
                'external_ref': reference,
                **default_vals,
            }
            vals_list.append(vals)

        self.webhook_line_ids.unlink()
        return self.env['integration.webhook.line'].create(vals_list)

    def prepare_webhook_routes(self):
        result = dict()
        routes = self._retrieve_webhook_routes()

        for controller_method, names in routes.items():
            for name_tuple in names:
                route = self._build_webhook_route(controller_method)
                key_tuple = (controller_method,) + name_tuple
                result[key_tuple] = route

        return result

    def _get_base_url_or_debug(self):
        debug_url = config.options.get('localhost_debug_url')
        if debug_url:
            return debug_url  # Fake url, just for localhost coding and bedug
        return self.get_base_url()

    def _build_webhook_route(self, controller_method):
        db_name = self.env.cr.dbname
        base_url = self._get_base_url_or_debug()
        return f'{base_url}/{db_name}/integration/{self.type_api}/{self.id}/{controller_method}'

    def _retrieve_webhook_routes(self):
        _logger.error('Webhook routes are not specified for the "%s".', self.name)
        return dict()

    @api.depends('type_api')
    def _compute_configuration_wizard_exists(self):
        for integration in self:
            try:
                # pylint: disable=pointless-statement
                integration_postfix = integration._get_configuration_postfix()
                self.env['configuration.wizard.' + integration_postfix]
                integration.is_configuration_wizard_exists = True
            except (KeyError, TypeError):
                integration.is_configuration_wizard_exists = False

    def _get_configuration_postfix(self):
        self.ensure_one()
        return self.type_api

    def action_active(self):
        self.ensure_one()
        self.action_check_connection(raise_success=False)
        self.state = 'active'

    def action_draft(self):
        self.ensure_one()
        self.state = 'draft'

    def action_check_connection(self, raise_success=True):
        self.ensure_one()
        adapter = self._build_adapter()

        try:
            connection_ok = adapter.check_connection()
        except Exception as e:
            raise UserError(e) from e

        if connection_ok:
            if raise_success:
                raise UserError(_('Connected'))
        else:
            raise UserError(_('Connection failed'))

    @api.depends('state')
    def _compute_is_installed_mrp(self):
        installed = self.sudo().env.ref('base.module_mrp').state == 'installed'
        for rec in self:
            rec.is_installed_mrp = installed

    @property
    def is_installed_website_sale(self):
        return self.sudo().env.ref('base.module_website_sale').state == 'installed'

    @property
    def is_installed_sale_product_configurator(self):
        return self.sudo().env.ref('base.module_sale_product_configurator').state == 'installed'

    def update_crons_activity(self):
        for integration in self:
            if not integration.receive_orders_cron_id:
                cron = integration._create_receive_orders_cron()
                integration.receive_orders_cron_id = cron

            if not integration.inventory_synchronization_cron_id:
                cron = integration._create_inventory_cron()
                integration.inventory_synchronization_cron_id = cron

            integration.receive_orders_cron_id.active = integration.state == 'active'
            integration.inventory_synchronization_cron_id.active = (
                integration.state == 'active' and
                integration.synchronize_all_inventory_periodically
            )

    def _create_receive_orders_cron(self):
        self.ensure_one()
        vals = {
            'name': f'Integration: {self.name} Receive Orders',
            'model_id': self.env.ref('integration.model_sale_integration').id,
            'numbercall': -1,
            'interval_type': 'minutes',
            'interval_number': 5,
            'code': f'model.browse({self.id}).integrationApiReceiveOrders()',
        }
        cron = self.env['ir.cron'].create(vals)
        return cron

    def _create_inventory_cron(self):
        self.ensure_one()

        # Calculating time in UTC for nextcall in 00:00:000 by users time zone
        tz = self.env.user.tz and pytz.timezone(self.env.user.tz) or pytz.utc
        now_user = datetime.now(tz=tz).replace(minute=0, second=0, tzinfo=None)
        now_utc = datetime.now().replace(minute=0, second=0)
        diff = now_user - now_utc
        nextcall = now_utc.replace(hour=0) - diff + timedelta(days=1)

        vals = {
            'name': f'Integration: {self.name} Synchronize inventory',
            'model_id': self.env.ref('integration.model_sale_integration').id,
            'numbercall': -1,
            'interval_type': 'days',
            'interval_number': 1,
            'code': f'model.browse({self.id}).integrationApiExportInventory()',
            'nextcall': nextcall.strftime('%Y-%m-%d %H:%M:%S'),
        }
        cron = self.env['ir.cron'].create(vals)
        return cron

    def unlink(self):
        self.receive_orders_cron_id.unlink()
        self.inventory_synchronization_cron_id.unlink()
        return super(SaleIntegration, self).unlink()

    def get_class(self):
        """It's just a stub."""
        return NoAPIClient

    @api.model
    def get_integrations(self, job, company):
        domain = [('state', '=', 'active')]

        if job:
            domain.append((f'{job}_job_enabled', '=', True))

        if company:
            domain.append(('company_id', '=', company.id))

        integrations = self.search(domain)
        return integrations

    def job_enabled(self, name):
        self.ensure_one()

        if self.state != 'active':
            return False

        job_enabled_field_name = f'{name}_job_enabled'
        result = self[job_enabled_field_name]
        return result

    @api.model_create_multi
    def create(self, vals):
        res = super().create(vals)
        res.write_settings_fields(vals)
        res.create_fields_mapping_for_integration()
        res.update_crons_activity()
        return self.browse(res.id)

    def write(self, vals):
        self.ensure_one()
        res = super().write(vals)
        ctx = self.env.context.copy()
        if not ctx.get('write_settings_fields'):
            res = self.write_settings_fields(vals)

        if 'synchronize_all_inventory_periodically' in vals or 'state' in vals:
            self.update_crons_activity()

        return res

    def create_fields_mapping_for_integration(self):
        ecommerce_fields = self.env['product.ecommerce.field']\
            .search([('type_api', '=', self.type_api), ('is_default', '=', True)])

        for field in ecommerce_fields:
            create_vals = {
                'ecommerce_field_id': field.id,
                'integration_id': self.id,
                'send_on_update': field.default_for_update,
            }
            self.env['product.ecommerce.field.mapping'].create(create_vals)

    def write_settings_fields(self, vals):
        self.ensure_one()

        res = True
        if 'type_api' in vals:
            settings_fields = self.get_default_settings_fields(vals['type_api'])
        else:
            settings_fields = self.get_default_settings_fields()

        if settings_fields is not None and settings_fields:
            exists_fields = self.field_ids.to_dictionary()
            settings_fields = self.convert_settings_fields(settings_fields)
            fields_list_to_add = [
                (0, 0, {
                    'name': field_name,
                    'description': field['description'],
                    'value': field['value'],
                    'eval': field['eval'],
                    'is_secure': field['is_secure'],
                })
                for field_name, field in settings_fields.items()
                if field_name not in exists_fields
            ]
            if fields_list_to_add:
                new_fields = {
                    'field_ids': fields_list_to_add
                }
                ctx = self.env.context.copy()
                ctx.update({'write_settings_fields': True})
                res = self.with_context(ctx).write(new_fields)

        return res

    def get_settings_value(self, key):
        self.ensure_one()
        field = self.get_settings_field(key)
        value = field.value
        return value

    def set_settings_value(self, key, value):
        self.ensure_one()
        field = self.get_settings_field(key)
        field.value = value

    def get_settings_field(self, key):
        self.ensure_one()

        field = self.field_ids.search([
            ('sia_id', '=', self.id),
            ('name', '=', key),
        ], limit=1)

        if field:
            return field

        # If field was not found the first time can be that this
        # is new setting and we need to add default value
        self.write_settings_fields({})

        field = self.field_ids.search([
            ('sia_id', '=', self.id),
            ('name', '=', key),
        ], limit=1)

        if not field:
            raise ValueError(f'Settings field with key = {key} is not found!')

        return field

    def convert_external_tax_to_odoo(self, tax_id):
        """Expected its own implementation for each integration."""
        return False

    @api.model
    def convert_settings_fields(self, settings_fields):
        return {
            field[0]: {
                'name': field[0],
                'description': field[1],
                'value': field[2],
                'eval': field[3] if len(field) > 3 else False,
                'is_secure': field[4] if len(field) > 4 else False,
            }
            for field in settings_fields
        }

    @staticmethod
    def get_external_block_limit():
        return IMPORT_EXTERNAL_BLOCK

    @api.model
    def get_default_settings_fields(self, type_api=None):
        return getattr(self.get_class(), 'settings_fields')

    def get_integration_location(self):
        self.ensure_one()

        if not self.location_ids:
            wh_ids = self.env['stock.warehouse'].search([
                ('company_id', '=', self.company_id.id)
            ])
            return wh_ids.mapped('lot_stock_id')

        return self.location_ids

    def initial_import_taxes(self):
        self.integrationApiImportTaxes()

    def initial_import_attributes(self):
        self.integrationApiImportAttributes()
        self.integrationApiImportAttributeValues()

    def initial_import_features(self):
        self.integrationApiImportFeatures()
        self.integrationApiImportFeatureValues()

    def initial_import_countries(self):
        self.integrationApiImportCountries()
        self.integrationApiImportStates()

    def integrationApiImportData(self):
        self.with_context(company_id=self.company_id.id).with_delay(
            description='Initial Import. Languages'
        ).integrationApiImportLanguages()

        self.with_context(company_id=self.company_id.id).with_delay(
            description='Initial Import. Shipping Methods'
        ).integrationApiImportDeliveryMethods()

        self.with_context(company_id=self.company_id.id).with_delay(
            description='Initial Import. Taxes'
        ).initial_import_taxes()

        self.with_context(company_id=self.company_id.id).with_delay(
            description='Initial Import. Payment Methods'
        ).integrationApiImportPaymentMethods()

        self.with_context(company_id=self.company_id.id).with_delay(
            description='Initial Import. Attributes'
        ).initial_import_attributes()

        self.with_context(company_id=self.company_id.id).with_delay(
            description='Initial Import. Features'
        ).initial_import_features()

        self.with_context(company_id=self.company_id.id).with_delay(
            description='Initial Import. Countries'
        ).initial_import_countries()

        self.with_context(company_id=self.company_id.id).with_delay(
            description='Initial Import. Categories'
        ).integrationApiImportCategories()

        self.with_context(company_id=self.company_id.id).with_delay(
            description='Initial Import. Sub-Statuses'
        ).integrationApiImportSaleOrderStatuses()

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Initial Import'),
                'message': 'Queue Jobs "Initial Import" are created',
                'type': 'success',
                'sticky': False,
            }
        }

    def action_import_master_data(self):
        return self.integrationApiImportData()

    def action_import_product_from_external(self):
        action = self._validate_product_templates(False)
        if action:
            return action

        self.with_context(company_id=self.company_id.id).with_delay(
            description='Initial Products Import: Prepare Product Batches'
        ).integrationApiImportProducts()

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Initial Products Import'),
                'message': 'Queue Jobs "Initial Products Import" are created',
                'type': 'success',
                'sticky': False,
            }
        }

    def action_create_products_in_odoo(self):
        action = self._validate_product_templates(False)
        if action:
            return action

        self.with_context(company_id=self.company_id.id).with_delay(
            description='Create Products In Odoo. Prepare Product Batches'
        ).integrationApiCreateProductsInOdoo()

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Create Products In Odoo'),
                'message': 'Queue Jobs "Create Products In Odoo" are created',
                'type': 'success',
                'sticky': False,
            }
        }

    def action_import_related_products(self):
        adapter = self._build_adapter()
        # Fetch data.
        adapter_products, template_router = adapter.get_products_for_accessories()

        model_name = 'product.template'
        ProductTemplateExternal = self.env[f'integration.{model_name}.external']
        ProductTemplateMapping = self.env[f'integration.{model_name}.mapping']
        mappings = self.env[f'integration.{model_name}.mapping']
        internal_field_name, external_field_name = ProductTemplateMapping._mapping_fields
        MessageWizard = self.env['message.wizard']

        # Create / update external and mappings.
        for product in adapter_products:
            name = product['name']
            # Get translation if name contains different languages
            if isinstance(name, dict) and name.get('language'):
                original = ProductTemplateExternal.get_original_name(name, self)

                if original:
                    name = original

            external_record = ProductTemplateExternal.create_or_update({
                'integration_id': self.id,
                'code': product['id'],
                'name': name,
                'external_reference': product.get('external_reference'),
            })
            external_record._post_import_external_one(product)

            mapping = ProductTemplateMapping.search([
                ('integration_id', '=', self.id),
                (external_field_name, '=', external_record.id),
            ])
            if not mapping:
                mapping = ProductTemplateMapping.create({
                    'integration_id': self.id,
                    external_field_name: external_record.id,
                })

            mappings |= mapping

        if not mappings:
            message_wizard = MessageWizard.create({
                'message': _('No related products to synchronize.'),
            })
            return message_wizard.run_wizard('integration_message_wizard_form')

        mappings_to_fix = mappings.filtered(lambda x: not getattr(x, internal_field_name))

        # Fix unmapped records if necessary. Format message.
        if mappings_to_fix:
            message = _(
                'Some of the related products are not yet synchronised to Odoo or not yet mapped '
                'to corresponding Odoo Products so it is not possible to import them. '
                'Please, make sure to launch products synchronisation again and make sure '
                'to map products in menu "Mappings --> Products" '
                '(or create them in Odoo by clicking "Import Products" button in the same menu):'
            )
            mapping_names = mappings_to_fix.mapped(f'{external_field_name}.display_name')

            html_message = f'<div>{message}</div>'
            html_names = f'<ul>{"".join([f"<li>{x}</li>" for x in mapping_names])}</ul>'

            message_wizard = MessageWizard.create({
                'message': str(mappings_to_fix.ids),
                'message_html': html_message + '<br/>' + html_names,
            })
            return message_wizard.run_wizard('integration_message_wizard_form_mapping_product')

        # Assign related products to the parent product.
        templates = self.env[model_name]
        for template_external_id, related_products_ids in template_router.items():
            template = templates.from_external(self, template_external_id, False)

            optional_product_ids = self.env[model_name]
            for product_id in related_products_ids:
                optional_product_ids |= templates.from_external(self, product_id, False)

            template.optional_product_ids = [(6, 0, optional_product_ids.ids)]
            templates |= template

        # Summary. Format message.
        mapping_names = list()
        base_url = self.sudo().env['ir.config_parameter'].get_param('web.base.url')
        pattern = (
            '<a href="%s/web#id=%s&model=%s&view_type=form" target="_blank">%s</a>'
        )

        def _format_optional_products(template):
            names = template.optional_product_ids.mapped('name')
            html_names = f'<ul>{"".join([f"<li>{x}</li>" for x in names])}</ul>'
            template_name = pattern % (base_url, template.id, model_name, template.name)
            return f'<li>{template_name + html_names}</li>'

        for template in templates:
            mapping_names.append(
                _format_optional_products(template)
            )

        message = _('The Products were synchronized:\n%s') % (f'<ul>{"".join(mapping_names)}</ul>')

        message_wizard = MessageWizard.create({
            'message': '',
            'message_html': message,
        })
        return message_wizard.run_wizard('integration_message_wizard_html_form')

    def integrationApiImportDeliveryMethods(self):
        external_records, adapter_external_data = self._import_external(
            'integration.delivery.carrier.external',
            'get_delivery_methods',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportTaxes(self):
        external_records, adapter_external_data = self._import_external(
            'integration.account.tax.external',
            'get_taxes',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportPaymentMethods(self):
        external_records, adapter_external_data = self._import_external(
            'integration.sale.order.payment.method.external',
            'get_payment_methods',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportLanguages(self):
        external_records, adapter_external_data = self._import_external(
            'integration.res.lang.external',
            'get_languages',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportAttributes(self):
        external_records, adapter_external_data = self._import_external(
            'integration.product.attribute.external',
            'get_attributes',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportAttributeValues(self):
        external_records, adapter_external_data = self._import_external(
            'integration.product.attribute.value.external',
            'get_attribute_values',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportFeatures(self):
        external_records, adapter_external_data = self._import_external(
            'integration.product.feature.external',
            'get_features',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportFeatureValues(self):
        external_records, adapter_external_data = self._import_external(
            'integration.product.feature.value.external',
            'get_feature_values',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportCountries(self):
        external_records, adapter_external_data = self._import_external(
            'integration.res.country.external',
            'get_countries',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportStates(self):
        external_records, adapter_external_data = self._import_external(
            'integration.res.country.state.external',
            'get_states',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportCategories(self):
        external_records, adapter_external_data = self._import_external(
            'integration.product.public.category.external',
            'get_categories',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportProducts(self):
        limit = self.get_external_block_limit()
        adapter = self._build_adapter()
        template_ids = adapter.get_product_template_ids()

        while template_ids:
            self.with_context(company_id=self.company_id.id).with_delay(
                description='Initial Products Import: '
                            'Import Products Batch (create external records + auto-matching)'
            ).import_external_product(template_ids[:limit])

            template_ids = template_ids[limit:]

    def integrationApiImportSaleOrderStatuses(self):
        external_records, adapter_external_data = self._import_external(
            'integration.sale.order.sub.status.external',
            'get_sale_order_statuses',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def run_create_products_in_odoo_by_blocks(self, external_templates):
        return external_templates.run_import_products(import_images=True)

    def integrationApiCreateProductsInOdoo(self):
        limit = self.get_external_block_limit()

        external_templates = self.env['integration.product.template.external'].search([
            ('integration_id', '=', self.id),
        ])

        while external_templates:
            self.with_context(company_id=self.company_id.id).with_delay(
                description='Create Products In Odoo. Prepare Products For Creating'
            ).run_create_products_in_odoo_by_blocks(external_templates[:limit])

            external_templates = external_templates[limit:]

    def integrationApiProductsValidationTest(self):
        return self._validate_product_templates(True)

    def _fetch_external_tax(self, tax_id):
        adapter = self._build_adapter()
        adapter_data = adapter.get_single_tax(tax_id)
        return adapter_data

    def _import_external_tax(self, tax_id):
        model_name = 'account.tax'
        adapter_external_data = self._fetch_external_tax(tax_id)
        external_record = self._import_external_record(
            self.env[f'integration.{model_name}.external'],
            adapter_external_data,
        )
        mapping = self.env[model_name].create_or_update_mapping(
            self,
            None,
            external_record,
        )
        tax = mapping._fix_unmapped_tax_one(external_data=adapter_external_data)
        return tax

    def _fetch_external_carrier(self, carrier_data):
        return carrier_data

    def _import_external_carrier(self, carrier_data):
        model_name = 'delivery.carrier'
        adapter_external_data = self._fetch_external_carrier(carrier_data)
        external_record = self._import_external_record(
            self.env[f'integration.{model_name}.external'],
            adapter_external_data,
        )
        mapping = self.env[model_name].create_or_update_mapping(
            self,
            None,
            external_record,
        )
        carrier = mapping._fix_unmapped_shipping_one()
        return carrier

    def import_external_product(self, template_ids):
        if not isinstance(template_ids, list):
            template_ids = [template_ids]

        template_ids = [str(x) for x in template_ids]

        ext_templates = self._build_adapter().get_product_templates(template_ids)

        ExternalTemplate = self.env['integration.product.template.external']
        ExternalVariant = self.env['integration.product.product.external']

        external_templates = ExternalTemplate
        external_variants = ExternalVariant

        for ext_template in ext_templates.values():
            external_template = self._import_external_record(ExternalTemplate, ext_template)
            external_templates |= external_template

            for ext_variant in ext_template.get('variants', []):
                external_variants |= self._import_external_record(ExternalVariant, ext_variant)

            external_template.try_map_template_and_variants(ext_template)

        return external_templates, external_variants

    def _import_external_record(self, external_model, external_data):
        name = external_data.get('name')

        # Get translation if name contains different languages
        if isinstance(name, dict) and name.get('language'):
            original = external_model.get_original_name(name, self)

            if original:
                name = original

        if not name:
            name = external_data['id']

        if not external_model._pre_import_external_check(external_data, self):
            return external_model

        result = external_model.create_or_update({
            'integration_id': self.id,
            'code': external_data['id'],
            'name': name,
            'external_reference': external_data.get('external_reference'),
        })
        result._post_import_external_one(external_data)
        return result

    def _import_external(self, model, method):
        self.ensure_one()
        adapter = self._build_adapter()
        adapter_method = getattr(adapter, method)
        adapter_external_data = adapter_method()

        external_records = self.env[model]
        for adapter_external_record in adapter_external_data:
            external_records |= self._import_external_record(
                external_records, adapter_external_record)

        external_records._post_import_external_multi(adapter_external_data)

        return external_records, adapter_external_data

    def export_template(self, template, *, export_images=False):
        self.ensure_one()
        adapter = self._build_adapter()

        results_list = []

        # First validate if product template is ready to be exported
        template.validate_in_odoo(self)

        template_for_export = template.to_export_format(self)
        # Now let's validate template in external system
        # In case we will be returned with external records to delete
        # we need to clean up and trigger export job again
        ext_records_to_delete = adapter.validate_template(template_for_export)
        if ext_records_to_delete:
            for rec_to_del in ext_records_to_delete:
                model_name = rec_to_del['model']
                ext_code = rec_to_del['external_id']
                ext_model = self.env[f'integration.{model_name}.external']
                ext_rec = ext_model.get_external_by_code(self, ext_code, False)
                if ext_rec:
                    ext_rec.unlink()
            # Trigger export of the same product template
            template.trigger_export(export_images, self)
            return _('Some products didn\'t exists in external system. '
                     'External records where cleaned up and export was triggered again ')

        # Now let's check if such product already exist in external system
        # so instead of creating new we can import existing one
        existing_external_product_id = adapter.find_existing_template(template_for_export)
        if existing_external_product_id:
            ExternalProduct = self.env['integration.product.template.external']
            external_product_record = ExternalProduct.create_or_update({
                'integration_id': self.id,
                'code': existing_external_product_id,
            })
            self.with_delay(description='Import Product').import_product(
                external_product_record,
                import_images=False,
            )
            return _('Existing Product found in external system with id %s. Triggering job to '
                     'import product instead of exporting it') % existing_external_product_id

        adapter_mappings = adapter.export_template(template_for_export)

        is_only_template = False
        external_product_template = False
        external_variants = self.env['integration.product.product.external']

        # If there is only product template exported (without variants)
        # Than there are only 2 mappings
        # and one of the mappings external_id ends with '-0'
        if len(adapter_mappings) == 2:
            is_only_template = any([b['external_id'].endswith('-0') for b in adapter_mappings])

        for adapter_mapping in adapter_mappings:
            is_variant = adapter_mapping['model'] == 'product.product'
            is_template = adapter_mapping['model'] == 'product.template'

            odoo_record = self.env[adapter_mapping['model']].browse(adapter_mapping['id'])
            extra_vals = dict(name=odoo_record.name)
            external_reference = adapter_mapping.get('external_reference', False)

            if not external_reference:
                if is_variant or (is_template and is_only_template):
                    external_reference = odoo_record.default_code

            extra_vals['external_reference'] = external_reference

            mapping = odoo_record.create_mapping(
                self,
                adapter_mapping['external_id'],
                extra_vals=extra_vals,
            )
            if is_template:
                external_product_template = mapping.external_template_id
            else:
                external_variants |= mapping.external_product_id

            external_variants.write({
                'external_product_template_id': external_product_template.id,
            })

        results_list.append(
            _('SUCCESS! Product Template "%s" was exported successfully. Product Template Code in '
              'external system is %s') % (template.name, external_product_template.code)
        )

        # In some cases export image/export inventory is failing.
        # But till the current moment we already may have created products in
        # external system via API. If Export image or export inventory fails for some reasons
        # (not so critical operations), then mapping will not be saved (rolled back) and
        # connector will not know that we just exported products. Hence we are enclosing both
        # image export and inventory export in try/except. To log potential problems and not to
        # rollback.
        # TODO: Remove this later
        # Explanation: Once automatic smart guessing mechanism is working in all connectors
        # remove this. Because smart guessing mechanism on repeated export will understand if
        # product already exists
        if export_images:
            results_list.append(LOG_SEPARATOR)
            try:
                self.export_images(template)
                results_list.append(
                    _('SUCCESS! Images for Product Template "%s" were exported '
                      'successfully.') % template.name
                )
            except Exception:
                buff = StringIO()
                traceback.print_exc(file=buff)
                _logger.error(buff.getvalue())
                results_list.append(
                    _('ERROR! Failed to export image for Product Template "%s". '
                      'Detailed Traceback: \n%s') % (template.name, buff.getvalue())
                )

        # Export inventory only for storable products and only if export inventory is enabled
        if template.type == 'product' and self.export_inventory_job_enabled:
            results_list.append(LOG_SEPARATOR)
            try:
                template.product_variant_ids.export_inventory(self)
                results_list.append(
                    _('SUCCESS! Stock Quantities for Product Template "%s" were exported '
                      'successfully.') % template.name
                )
            except Exception:
                buff = StringIO()
                traceback.print_exc(file=buff)
                _logger.error(buff.getvalue())
                results_list.append(
                    _('ERROR! Failed to export stock quantities for Product Template "%s". '
                      'Detailed Traceback: \n%s') % (template.name, buff.getvalue())
                )
        # Joining all results, so they will be visible in Job results log
        return '\n\n'.join(results_list)

    def calculate_field_value(self, odoo_object, ecommerce_field):
        self.ensure_one()
        converter_method = getattr(self, '_get_{}_value'.format(ecommerce_field.value_converter))
        if not converter_method:
            raise UserError(
                _(
                    'There is no method defined for converter %s'
                ) % ecommerce_field.value_converter
            )
        return converter_method(odoo_object, ecommerce_field)

    def _get_simple_value(self, odoo_object, ecommerce_field):
        return getattr(odoo_object, ecommerce_field.odoo_field_id.name)

    def _get_translatable_field_value(self, odoo_object, ecomm_field):
        return self.convert_translated_field_to_integration_format(odoo_object,
                                                                   ecomm_field.odoo_field_id.name)

    def _get_python_method_value(self, odoo_object, ecommerce_field):
        custom_python_method = getattr(odoo_object, ecommerce_field.method_name)
        if not custom_python_method:
            raise UserError(
                _(
                    'There is no method %s defined for object %s'
                ) % (ecommerce_field.method_name, odoo_object._name)
            )
        return custom_python_method(self)

    def convert_translated_field_to_integration_format(self, record, field):
        self.ensure_one()

        language_mappings = self.env['integration.res.lang.mapping'].search([
            ('integration_id', '=', self.id)
        ])

        translations = {}
        for language_mapping in language_mappings:
            external_code = language_mapping.external_language_id.code
            translations[external_code] = getattr(
                record.with_context(lang=language_mapping.language_id.code),
                field,
            )

        return translations

    def export_images(self, template):
        self.ensure_one()
        adapter = self._build_adapter()
        export_images_data = template.to_images_export_format(self)
        adapter.export_images(export_images_data)

    def export_tracking(self, pickings):
        self.ensure_one()

        order = pickings.mapped('sale_id')
        assert len(order) == 1

        sale_order_id = order.to_external(self)
        tracking_data = pickings.to_export_format_multi(self)

        adapter = self._build_adapter()
        result = adapter.export_tracking(sale_order_id, tracking_data)
        # After successful tracking export, add corresponding flag to the picking
        if result:
            pickings.write({
                'tracking_exported': True,
            })
        return result

    def export_sale_order_status(self, order):
        self.ensure_one()

        adapter = self._build_adapter()
        vals = order._prepare_vals_for_sale_order_status()
        res = adapter.export_sale_order_status(vals)
        return f'{res}\n\nExported Data:\n\n{json.dumps(vals, indent=4)}'

    def export_attribute(self, attribute):
        self.ensure_one()
        adapter = self._build_adapter()

        to_export = attribute.to_export_format(self)
        code = adapter.export_attribute(to_export)

        attribute.create_mapping(self, code, extra_vals={'name': attribute.name})

        return code

    def export_attribute_value(self, attribute_value):
        self.ensure_one()
        adapter = self._build_adapter()

        attribute_value_export = attribute_value.to_export_format(self)
        attribute_code = attribute_value_export.get('attribute')
        if not attribute_code:
            raise UserError(
                _('External attribute code cannot be empty. '
                  'Attribute Value: %s') % attribute_value.name
            )

        attribute_value_code = adapter.export_attribute_value(attribute_value_export)

        attribute_value_mapping = attribute_value.create_mapping(
            self,
            attribute_value_code,
            extra_vals={'name': attribute_value.name},
        )
        external_attribute = self.env['integration.product.attribute.external'].search([
            ('code', '=', attribute_code),
            ('integration_id', '=', self.id),
        ])

        external_attribute_value = attribute_value_mapping.external_attribute_value_id
        external_attribute_value.external_attribute_id = external_attribute.id

        return attribute_value_code

    def export_feature(self, feature):
        self.ensure_one()
        adapter = self._build_adapter()

        to_export = feature.to_export_format(self)
        code = adapter.export_feature(to_export)

        feature.create_mapping(self, code, extra_vals={'name': feature.name})

        return code

    def export_feature_value(self, feature_value):
        self.ensure_one()
        adapter = self._build_adapter()

        feature_value_export = feature_value.to_export_format(self)
        feature_code = feature_value_export.get('feature_id')
        if not feature_code:
            raise UserError(
                _('External feature code cannot be empty. '
                  'Feature Value: %s') % feature_value.name
            )

        feature_value_code = adapter.export_feature_value(feature_value_export)

        feature_value_mapping = feature_value.create_mapping(
            self,
            feature_value_code,
            extra_vals={'name': feature_value.name},
        )
        external_feature = self.env['integration.product.feature.external'].search([
            ('code', '=', feature_code),
            ('integration_id', '=', self.id),
        ])

        external_feature_value = feature_value_mapping.external_feature_value_id
        external_feature_value.external_feature_id = external_feature.id

        return feature_value_code

    def export_category(self, category):
        self.ensure_one()
        adapter = self._build_adapter()

        code = adapter.export_category(category.to_export_format(self))
        category.create_mapping(self, code, extra_vals={'name': category.name})

        return code

    def _build_adapter(self):
        self.ensure_one()
        # Before building adapter make sure all default settings are there
        self.write_settings_fields({})
        settings = self.to_dictionary()
        adapter = settings['class'](settings)
        adapter._env = self.env
        adapter._integration_id = self.id
        adapter._integration_name = self.name
        return adapter

    def to_dictionary(self):
        self.ensure_one()
        return {
            'name': self.name,
            'type_api': self.type_api,
            'class': self.get_class(),
            'fields': self.field_ids.to_dictionary(),
            'data_block_size': int(self.env['ir.config_parameter'].sudo().get_param(
                'integration.import_data_block_size'))
        }

    def integrationApiReceiveOrders(self):
        self.ensure_one()

        adapter = self._build_adapter()
        input_files = adapter.receive_orders()

        created_input_files = self.env['sale.integration.input.file']
        for input_file in input_files:
            self._validate_input_file_format(input_file)

            external_id = input_file['id']
            exists = self.env['sale.integration.input.file'].search([
                ('name', '=', external_id),
                ('si_id', '=', self.id),
            ], limit=1)

            if exists:
                continue

            input_file_data = input_file['data']
            input_file_json = json.dumps(input_file_data, indent=4)

            created_input_files += self.env['sale.integration.input.file'].create({
                'name': external_id,
                'si_id': self.id,
                'raw_data': input_file_json,
            })

        self.update_last_receive_orders_datetime_to_now()

        return created_input_files

    def integrationApiExportInventory(self):
        products = self.env['product.product'].search([('integration_ids.id', '=', self.id)])
        products.export_inventory_by_jobs(self)

    def update_last_receive_orders_datetime_to_now(self):
        self.last_receive_orders_datetime = datetime.now()

    def trigger_create_order(self, input_files):
        jobs = self.env['queue.job']

        for input_file in input_files:
            integration = input_file.si_id
            integration = integration.with_context(company_id=integration.company_id.id)
            job = integration.with_delay(description='Import Order')\
                .create_order_from_input(input_file)
            jobs += job.db_record()

        return jobs

    def trigger_link_all(self):
        """Link integration to the all products."""
        self._apply_to_all(4)

    def trigger_unlink_all(self):
        """Unlink integration from the all products."""
        self._apply_to_all(3)

    def trigger_link_mapped_products(self):
        """Link all mapped products."""
        for integration in self:
            products = self.env['integration.product.product.mapping'].\
                search([('integration_id', '=', integration.id), ('product_id', '!=', False)])\
                .mapped('product_id')
            if products:
                products.with_context(skip_product_export=True)\
                    .write({'integration_ids': [(4, integration.id, 0)]})

    def _apply_to_all(self, value):
        """
        `value` in accordance to ORM API:
            0: adds a new record created from the provided value dict.
            1: updates an existing record of id with the values in values.
            2: removes the record of id from the set, then deletes it (from the database).
            3: removes the record of id from the set, but does not delete it.
            4: adds an existing record of id to the set.
            5: removes all records from the set.
            6: replaces all existing records in the set by the ids list.
        """
        vals = dict(integration_ids=[(value, integration.id) for integration in self])
        product_variants = self.env['product.product'].search([])
        product_variants.write(vals)

    def parse_order(self, input_file):
        self.ensure_one()

        adapter = self._build_adapter()

        input_file_data = input_file.to_dict()
        order_data = adapter.parse_order(
            input_file_data,
        )

        self._validate_order_format(order_data)

        return order_data

    def create_order_from_input(self, input_file):
        self.ensure_one()

        order_data = self.parse_order(input_file)

        sof = self.env['integration.sale.order.factory'].with_company(self.company_id)
        order = sof.create_order(self, order_data)

        input_file.state = 'done'
        input_file.order_id = order.id

        job_kwargs = order._build_workflow_job_kwargs()
        job_kwargs['description'] = (
            f'Create Integration Workflow: [{self.id}][{order.display_name}]'
        )
        order.with_delay(**job_kwargs)\
            ._build_and_run_integration_workflow(order_data, input_file.id)
        return order

    def integrationApiCreateOrders(self):  # Seems this one not used currently
        self.ensure_one()

        input_files = self.env['sale.integration.input.file'].search([
            ('si_id', '=', self.id),
            ('state', '=', 'draft'),
        ])

        orders = self.env['sale.order']
        for input_file in input_files:
            orders += self.create_order_from_input(input_file)

        return orders

    @api.model
    def systray_get_integrations(self):
        integrations = self.search([
            ('state', '=', 'active'),
        ])

        result = []
        for integration in integrations:
            failed_jobs_count = self.env['queue.job'].sudo().search_count([
                ('model_name', '=', 'sale.integration'),
                ('func_string', 'like', f'{self._name}({integration.id},)'),
                ('state', '=', 'failed'),
                ('company_id', '=', integration.company_id.id)
            ])

            missing_mappings_count = 0
            for model_name in self.env:
                is_mapping_model = (
                    model_name.startswith('integration.')
                    and model_name.endswith('.mapping')
                    and model_name not in MAPPING_EXCEPT_LIST
                )
                if not is_mapping_model:
                    continue

                mapping_model = self.env[model_name]
                internal_field_name, external_field_name = mapping_model._mapping_fields
                missing_mappings = mapping_model.search_count([
                    ('integration_id', '=', integration.id),
                    (internal_field_name, '=', False),
                    (external_field_name, '!=', False),
                ])

                missing_mappings_count += missing_mappings

            integration_stats = {
                'name': integration.name,
                'failed_jobs_count': failed_jobs_count,
                'missing_mappings_count': missing_mappings_count,
            }
            result.append(integration_stats)

        return result

    def _validate_order_format(self, order):
        address_schema = {
            'id': {'type': 'string'},
            'person_name': {'type': 'string'},
            'email': {'type': 'string', 'required': False},
            'language': {'type': 'string', 'required': False},
            'person_id_number': {'type': 'string', 'required': False},
            'company_name': {'type': 'string', 'required': False},
            'company_reg_number': {'type': 'string', 'required': False},
            'street': {'type': 'string', 'required': False},
            'street2': {'type': 'string', 'required': False},
            'city': {'type': 'string', 'required': False},
            'country': {'type': 'string', 'required': False},
            'country_code': {'type': 'string', 'required': False},
            'state': {'type': 'string', 'required': False},
            'state_code': {'type': 'string', 'required': False},
            'zip': {'type': 'string', 'required': False},
            'phone': {'type': 'string', 'required': False},
            'mobile': {'type': 'string', 'required': False},
        }
        line_schema = {
            'id': {'type': 'string'},
            'product_id': {'type': 'string'},
            'odoo_variant_id': {'type': 'number', 'required': False},
            'product_uom_qty': {'type': 'number', 'required': False},
            'taxes': {
                'type': 'list',
                'schema': {'type': 'string'},
                'required': False,
            },
            'price_unit': {'type': 'number', 'required': False},
            'price_unit_tax_incl': {'type': 'number', 'required': False},
            'discount': {'type': 'number', 'required': False},
        }
        payment_transaction_schema = {
            'transaction_id': {'type': 'string', 'required': True},
            'transaction_date': {'type': 'string', 'required': True},
            'amount': {'type': 'number', 'required': True},
            'currency': {'type': 'string', 'required': False},
        }
        delivery_schema = {
            'carrier': {'type': 'dict'},
            'shipping_cost': {'type': 'float'},
            'shipping_cost_tax_excl': {'type': 'float', 'required': False},
            'carrier_tax_rate': {'type': 'float', 'required': False},
            'carrier_tax_ids': {
                'type': 'list',
                'schema': {'type': 'string'},
                'required': False,
            },
        }
        discount_schema = {
            'total_discounts_tax_incl': {'type': 'float', 'required': False},
            'total_discounts_tax_excl': {'type': 'float', 'required': False},
        }
        gift_schema = {
            'do_gift_wrapping': {'type': 'boolean', 'required': False},
            'wrapping_tax_behavior': {'type': 'string', 'required': False},
            'total_wrapping_tax_incl': {'type': 'float', 'required': False},
            'total_wrapping_tax_excl': {'type': 'float', 'required': False},
            'recycled_packaging': {'type': 'boolean', 'required': False},
            'wrapping_tax_ids': {
                'type': 'list',
                'schema': {'type': 'string'},
                'required': False,
            },
        }

        order_schema = {
            'id': {'type': 'string'},
            'ref': {'type': 'string'},
            'date_order': {'type': 'string', 'required': False},
            'current_order_state': {'type': 'string', 'required': False},
            'customer': {
                'type': 'dict',
                'schema': address_schema,
                'required': False,
            },
            'shipping': {
                'type': 'dict',
                'schema': address_schema,
                'required': False,
            },
            'billing': {
                'type': 'dict',
                'schema': address_schema,
                'required': False,
            },
            'lines': {
                'type': 'list',
                'schema': {
                    'type': 'dict',
                    'schema': line_schema,
                },
            },
            'payment_method': {'type': 'string'},
            'payment_transactions': {
                'required': False,
                'type': 'list',
                'schema': {
                    'type': 'dict',
                    'schema': payment_transaction_schema,
                },
            },
            'delivery_data': {
                'type': 'dict',
                'schema': delivery_schema,
            },
            'discount_data': {
                'type': 'dict',
                'schema': discount_schema,
            },
            'gift_data': {
                'type': 'dict',
                'schema': gift_schema,
            },
            'currency': {'type': 'string', 'required': False},
            'amount_total': {'type': 'float', 'required': False},
            'carrier_tax_behavior': {'type': 'string', 'required': False},
        }

        self._validate_by_schema(order, order_schema)

    def _validate_input_file_format(self, input_file):
        input_file_schema = {
            'id': {'type': 'string'},
            'data': {'type': 'dict'},
        }
        self._validate_by_schema(input_file, input_file_schema)

    def _validate_by_schema(self, data, schema):
        v = Validator(require_all=True, allow_unknown=True)
        valid = v.validate(data, schema)

        if not valid:
            raise Exception(v.errors)

    @api.model
    def _get_test_method(self):
        return [
            (method, method.replace('integrationApi', ''))
            for method in dir(self)
            if method.startswith('integrationApi') and callable(getattr(self, method))
        ]

    def test_job(self):
        method_name = self.test_method
        if not method_name:
            raise UserError(
                _(
                    'You should select test method in dropdown above, before clicking the button.'
                )
            )
        test_method = getattr(self, method_name, None)
        if test_method:
            return test_method()
        return True

    def _validate_product_templates(self, show_message=False):
        adapter = self._build_adapter()
        formatter = HtmlWrapper(self, adapter=adapter)

        tmpl_hub = adapter.get_templates_and_products_for_validation_test()

        template_ids, variant_ids = tmpl_hub.get_empty_ref_ids()
        duplicated_ref = tmpl_hub.get_dupl_refs()
        duplicated_bar = tmpl_hub.get_dupl_barcodes()

        if any((template_ids, variant_ids, duplicated_ref, duplicated_bar)):
            formatter.add_title(_('E-COMMERCE SYSTEM'))

        if template_ids:
            formatter.add_sub_block_for_external_product_list(
                _('Product IDs without reference in e-Commerce System:'),
                template_ids,
            )

        if variant_ids:
            formatter.add_sub_block_for_external_product_list(
                _('Product variants IDs without reference in e-Commerce System:'),
                variant_ids,
            )

        if duplicated_ref:
            formatter.add_sub_block_for_external_product_dict(
                _('Duplicated references in e-Commerce System:'),
                duplicated_ref,
            )

        if duplicated_bar:
            formatter.add_sub_block_for_external_product_dict(
                _('Duplicated barcodes in e-Commerce System:'),
                duplicated_bar,
            )

        # Test Odoo products
        odoo_variant_ids = self.env['product.product'].search_read(
            [],
            fields=[
                'id', 'barcode', 'default_code', 'product_tmpl_id',
            ],
        )

        tmpl_hub_odoo = tmpl_hub.__class__.from_odoo(odoo_variant_ids)

        __, variant_odoo_ids = tmpl_hub_odoo.get_empty_ref_ids()
        duplicated_ref_odoo = tmpl_hub_odoo.get_dupl_refs()
        duplicated_bar_odoo = tmpl_hub_odoo.get_dupl_barcodes()

        if any((variant_odoo_ids, duplicated_ref_odoo, duplicated_bar_odoo)):
            formatter.add_title(_('ODOO SYSTEM'))

        if variant_odoo_ids:
            formatter.add_sub_block_for_internal_variant_list(
                _('Product variants IDs without reference in Odoo:'),
                variant_odoo_ids,
            )

        if duplicated_ref_odoo:
            formatter.add_sub_block_for_internal_variant_dict(
                _('Duplicated references in Odoo:'),
                duplicated_ref_odoo,
            )

        if duplicated_bar_odoo:
            formatter.add_sub_block_for_internal_variant_dict(
                _('Duplicated barcodes in Odoo:'),
                duplicated_bar_odoo,
            )

        if not formatter.has_message and show_message:
            raise UserError(_('All products are correct.'))

        if formatter.has_message:
            message_wizard = self.env['message.wizard'].create({
                'message': 'Warning!',  # Hidden field
                'message_html': formatter.dump(),
            })
            return message_wizard.run_wizard('integration_message_wizard_validate_template_form')

        return False

    @raise_requeue_job_on_concurrent_update
    def import_product(self, external_template, import_images=False):
        self.ensure_one()

        adapter = self._build_adapter()

        ext_template, ext_products, ext_bom_components, images = adapter.get_product_for_import(
            external_template.code,
            import_images=import_images,
        )

        try:
            return external_template.import_one_product(
                ext_template,
                ext_products,
                ext_bom_components,
                images,
            )
        except OperationalError:
            raise
        except Exception as ex:
            raise ValidationError(
                _('%s\n\nTemplate:\n\t%s\n\nVariants:\n\t%s\n\nBOMS:\n\t%s')
                % (ex.args[0], ext_template, ext_products, ext_bom_components)
            )

    def action_run_configuration_wizard(self):
        if not self.is_configuration_wizard_exists:
            return

        integration_postfix = self._get_configuration_postfix()
        configuration_wizard = self.env['configuration.wizard.' + integration_postfix].search(
            [('integration_id', '=', self.id)],
            limit=1,
        )

        if not configuration_wizard:
            configuration_wizard = configuration_wizard.create({'integration_id': self.id})

        configuration_wizard.init_configuration()

        return configuration_wizard.get_action_view()

    @api.constrains("import_payments")
    def check_possibility_to_import_payments(self):
        for record in self:
            if 'account_payment_ids' not in self.env['sale.order']._fields \
                    and record.import_payments:
                module_link = 'https://github.com/OCA/sale-workflow/tree/%s/sale_advance_payment'
                module_link = module_link % release.major_version
                raise ValidationError(_('It is not possible to enable importing of the payments'
                                        ' in Odoo. You need to install this free module to make '
                                        'it work. Link to the module %s ') % module_link
                                      )

    def _should_link_parent_contact(self):
        # Since we are having `external_company_name` this one may de dropped.
        return True

    def force_set_inactive(self):
        return {'active': False}

    def _set_zero_time_zone(self, external_date):
        """
        Set time zone to UTC+0
        :param external_date - datetime class from dateutil
        :return: datetime object with time zone UTC+0
        """
        if external_date.tzinfo is not None:
            utc_data = external_date.astimezone(pytz.utc)
            return datetime.strftime(utc_data, DATETIME_FORMAT)
        return external_date
