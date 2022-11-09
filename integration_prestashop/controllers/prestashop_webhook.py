#  See LICENSE file for full copyright and licensing details.

import logging

from odoo.http import Controller, route
from odoo.addons.integration.controllers.integration_webhook import IntegrationWebhook

from ..prestashop_api import PRESTASHOP


_logger = logging.getLogger(__name__)


class PrestashopWebhook(Controller, IntegrationWebhook):

    _kwargs = {
        'type': 'json',
        'auth': 'none',
        'methods': ['POST'],
    }

    """
    headers = {
        X-Forwarded-Host: ventor-dev-integration-webhooks-test-15.odoo.com
        X-Forwarded-For: 141.95.36.76
        X-Forwarded-Proto: https
        X-Real-Ip: 141.95.36.76
        Connection: close
        Content-Length: 11369
        User-Agent: Httpful/0.2.20 (cURL/7.64.0 PHP/7.3.27-9+0~20210227.82+debian10~1.gbpa4a3d6
                    (Linux) Apache/2.4.38 (Debian) Mozilla/5.0 (X11; Linux x86_64)
                    AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.0.0 Safari/537.36)
        Content-Type: application/json
        Accept: */*; q=0.5, text/plain; q=0.8, text/html;level=3;
        X-Secure-Key: GHDGBKC15DIDXMXFXZHYUWXBPAEOGEAT
        X-Hook: actionProductUpdate
    }
    """

    @property
    def integration_type(self):
        return PRESTASHOP

    @route(f'/<string:dbname>/integration/{PRESTASHOP}/<int:integration_id>/orders', **_kwargs)
    def prestashop_receive_orders(self, *args, **kw):
        """
        Expected methods:
            actionOrderHistoryAddAfter (Order Status Updated)
            actionValidateOrder (Order Created)
        """
        _logger.info('Call prestashop webhook controller: receive_orders()')

        self.set_integration(*args, **kw)

        is_valid_webhook = self.verify_webhook(*args, **kw)
        if not is_valid_webhook:
            return

        is_done_action = self._run_method_from_header()
        return is_done_action

    def actionValidateOrder(self):
        """
        Order Created
        Method is not implemented
        """
        _logger.info('Call prestashop webhook controller: actionValidateOrder()')
        pass

    def actionOrderHistoryAddAfter(self):
        """
        Order Status Updated
        """
        _logger.info('Call prestashop webhook controller actionOrderHistoryAddAfter()')

        post_data = self._get_post_data()

        order_code = post_data['order']['id']
        reference = post_data['order']['reference']
        order = self.env['sale.order'].from_external(self.integration, order_code, False)
        if not order:
            _logger.error(
                'Prestashop Order not found, code=%s (reference=%s)', order_code, reference
            )
            return False

        status_code = post_data['order']['current_state']
        sub_status_id = self.env['sale.order.sub.status']\
            .from_external(self.integration, status_code, False)
        if not sub_status_id:
            _logger.error('Sub status not found for code: %s' % status_code)
            return False

        order.sub_status_id = sub_status_id

        if self.integration.run_action_on_cancel_so \
                and sub_status_id == self.integration.sub_status_cancel_id:
            job_kwargs = order._build_workflow_job_kwargs()
            job_kwargs['description'] = 'Integration Cancel Order'

            order.with_delay(**job_kwargs)._integration_action_cancel()
            return True

        return False

    def _check_webhook_digital_sign(self, adapter):
        return True  # TODO

    def _get_hook_name_method(self):
        headers = self._get_headers()
        header_name = self._get_hook_name_header()
        return headers[header_name]

    @staticmethod
    def _get_hook_name_header():
        return 'X-Hook'

    @staticmethod
    def _get_hook_shop_header():
        return 'X-Forwarded-Host'

    @staticmethod
    def _get_essential_headers():
        return [
            'X-Hook',
            'X-Secure-Key',
            'X-Forwarded-Host',
        ]

    def get_shop_domain(self):
        # TODO: now it's just a `stub`.
        # Method returns what the `webhook validator` expect for.
        # We need a header kind of the shopify `X-Shopify-Shop-Domain`
        adapter = self.integration._build_adapter()
        settings_url = adapter.get_settings_value('url')
        return settings_url
