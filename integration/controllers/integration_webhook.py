#  See LICENSE file for full copyright and licensing details.

import json
import logging

from psycopg2 import Error

from odoo.http import request
from odoo import api, registry, SUPERUSER_ID
from odoo.addons.integration.models.sale_integration import DEFAULT_LOG_LABEL, LOG_SEPARATOR


_logger = logging.getLogger(__name__)


class IntegrationWebhook:

    @property
    def env(self):
        if hasattr(self, '_env'):
            return self._env
        return None

    @env.setter
    def env(self, value):
        self._env = value

    @property
    def integration(self):
        if hasattr(self, '_integration'):
            return self._integration
        return None

    @integration.setter
    def integration(self, value):
        self._integration = value

    @property
    def integration_type(self):
        return None

    def _build_env(self, *args, **kw):
        request.session.db = kw.get('dbname')
        environment = request.env(user=SUPERUSER_ID)
        self.env = environment

    def set_integration(self, *args, **kw):
        """The first method for all child http-routes calling."""
        self._build_env(*args, **kw)

        integration_id = kw.get('integration_id')
        integration = self.env['sale.integration'].sudo().browse(integration_id).exists()
        assert self.integration_type
        integration_filter = integration.filtered(lambda x: x.type_api == self.integration_type)
        if not integration_filter:
            _logger.error('Webhook unrecognized integration.')

        self.integration = integration_filter

        _logger.info(
            'Integration webhook: %s, type-api="%s", controller-integration-type="%s".',
            str(integration),
            integration.type_api,
            self.integration_type,
        )
        if integration_filter.save_webhook_log:
            self._save_log(*args, **kw)
        return self.integration

    def set_order(self, key=None):
        order_key = key or 'id'
        post_data = self._get_post_data()
        order_id = str(post_data[order_key])

        order = self.env['sale.order'].from_external(self.integration, order_id, False)
        if not order:
            _logger.info('External Order not found, id=%s', order_id)
            if self._is_new_order():
                self.order_id = order
                return True

        self.order_id = order  # Save order as attribute of the controller instance
        return self.order_id

    def get_webhook_topic(self):
        headers = self._get_headers()
        topic_header = self._get_hook_name_header()
        return headers.get(topic_header, False)

    def check_essential_headers(self):
        headers = self._get_headers()
        essential_headers = self._get_essential_headers()
        return all(headers.get(x) for x in essential_headers)

    def get_shop_domain(self):
        headers = self._get_headers()
        shop_domain_header = self._get_hook_shop_header()
        return headers.get(shop_domain_header, False)

    def verify_webhook(self, *args, **kw):
        try:
            return self._verify_webhook(*args, **kw)
        except Exception as ex:
            _logger.error(ex)
            return False

    def _verify_webhook(self, *args, **kw):
        name = self.integration.name

        # 1. Verify integration activation
        if self.integration.state != 'active':
            _logger.info('%s integration is inactive.', name)
            return False

        # 2. Verify headers
        headers_ok = self.check_essential_headers()

        if not headers_ok:
            _logger.error('%s webhook invalid headers.', name)
            return False

        # 3. Verify forwarded host
        shop_domain = self.get_shop_domain()
        adapter = self.integration._build_adapter()
        settings_url = adapter._truncate_settings_url()

        if settings_url not in shop_domain:
            _logger.error('%s webhook invalid shop domain "%s".', name, shop_domain)
            return False

        # 4. Verify integration webhook-lines
        if not self.integration.webhook_line_ids:
            _logger.warning('%s webhooks not specified.', name)
            return False

        # 5. Verify webhook-line activation
        topic = self.get_webhook_topic()
        webhook_line_id = self.integration.webhook_line_ids\
            .filtered(lambda x: x.technical_name == topic)

        if not webhook_line_id.is_active:
            _logger.warning('Disabled %s webhook in Odoo "%s".', name, topic)
            return False

        # 6. Verify webhook digital sign
        sign_ok = self._check_webhook_digital_sign(adapter)
        if not sign_ok:
            _logger.error('Wrong %s webhook digital signature.', name)
            return False

        _logger.info('%s: webhook has been verified.', name)
        return True

    def _get_headers(self):
        return request.httprequest.headers

    def _get_post_data(self):
        return json.loads(request.httprequest.data)

    def _check_webhook_digital_sign(self, adapter):
        raise NotImplementedError

    @staticmethod
    def _new_order_method_name():
        raise NotImplementedError

    @staticmethod
    def _get_hook_name_header():
        raise NotImplementedError

    @staticmethod
    def _get_essential_headers():
        raise NotImplementedError

    @staticmethod
    def _get_hook_shop_header():
        raise NotImplementedError

    def _get_hook_name_method(self):
        raise NotImplementedError

    def _prepare_workflow_data(self):
        raise NotImplementedError

    def _save_log(self, *args, **kw):
        message_dict = {
            'ARGS: ': args,
            'KWARGS: ': kw,
            'HEADERS: ': dict(self._get_headers()),
            'POST-DATA: ': self._get_post_data(),
        }
        message_data = json.dumps(message_dict, indent=4)
        self._print_debug_data(message_data)

        vals = {
            'name': DEFAULT_LOG_LABEL,
            'type': 'client',
            'level': 'DEBUG',
            'dbname': self.env.cr.dbname,
            'message': message_data,
            'path': self.__module__,
            'func': self.__class__.__name__,
            'line': self.integration.name,
        }

        try:
            db_registry = registry(self.env.cr.dbname)
            with db_registry.cursor() as new_cr:
                new_env = api.Environment(new_cr, SUPERUSER_ID, {})
                log = new_env['ir.logging'].create(vals)
        except Error:
            log = self.env['ir.logging']

        return log

    def _print_debug_data(self, message_data):
        _logger.info(LOG_SEPARATOR)
        _logger.info('%s WEBHOOK DEBUG', self.integration_type)
        _logger.info(message_data)
        _logger.info(LOG_SEPARATOR)

    def _is_new_order(self):
        method_name_from_header = self._get_hook_name_method()
        new_order_method_name = self._new_order_method_name()
        return method_name_from_header == new_order_method_name

    def _run_method_from_header(self):
        name_method = self._get_hook_name_method()
        if not hasattr(self, name_method):
            _logger.info('Hook method "%s" for %s not found!', name_method, self.integration_type)
            return False
        return getattr(self, name_method)()

    def _get_order_sub_status(self, key):
        post_data = self._get_post_data()
        status_code = post_data.get(key, '')

        sub_status_id = self.env['sale.order.sub.status']\
            .from_external(self.integration, status_code, False)

        if not sub_status_id:
            _logger.error('Sub status not found for code: %s' % status_code)

        return sub_status_id

    def _receive_order_generic(self, *args, **kw):
        integration = self.set_integration(*args, **kw)
        if not integration:
            return False

        is_valid_webhook = self.verify_webhook(*args, **kw)
        if not is_valid_webhook:
            return is_valid_webhook

        order = self.set_order()
        if not order:
            return False

        is_done_action = self._run_method_from_header()
        return is_done_action

    def _receive_shipment_generic(self, *args, **kw):
        integration = self.set_integration(*args, **kw)
        if not integration:
            return False

        is_valid_webhook = self.verify_webhook(*args, **kw)
        if not is_valid_webhook:
            return is_valid_webhook

        order = self.set_order(key='order_id')
        if not order:
            return False

        is_done_action = self._run_method_from_header()
        return is_done_action

    def _cancel_order_generic(self):
        order = self.order_id
        job_kwargs = order._build_workflow_job_kwargs()
        job_kwargs['description'] = 'Integration Cancel Order'

        order.with_delay(**job_kwargs)._integration_action_cancel_no_dispatch()
        return True

    def _build_and_run_order_pipeline_generic(self, data=None):
        order = self.order_id
        order_data = data or self._prepare_workflow_data()
        ctx = dict(default_skip_dispatch=True)
        pipeline = order.integration_pipeline

        if not pipeline:
            return order.with_context(**ctx)._build_and_run_integration_workflow(order_data)
        return pipeline.with_context(**ctx)._update_and_re_run_pipeline(order_data)
