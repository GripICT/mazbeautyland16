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
        _logger.info('Call prestashop webhook controller: prestashop_receive_orders()')
        return self._receive_order_generic(*args, **kw)

    def actionValidateOrder(self):
        """Order Created"""
        _logger.info(
            'Call prestashop webhook controller: actionValidateOrder(). '
            'Not Implemented.'
        )
        pass

    def actionOrderHistoryAddAfter(self):
        """Order Status Updated"""
        _logger.info('Call prestashop webhook controller actionOrderHistoryAddAfter()')

        sub_status_id = self._get_order_sub_status('current_state')
        if not sub_status_id:
            return False

        if sub_status_id == self.integration.sub_status_cancel_id:  # TODO: unnecessary `if`
            return self._cancel_order_generic()

        return self._build_and_run_order_pipeline_generic()

    def get_shop_domain(self):
        # TODO: now it's just a `stub`.
        # Method returns what the `webhook validator` expect for.
        # We need a header kind of the shopify `X-Shopify-Shop-Domain`
        adapter = self.integration._build_adapter()
        settings_url = adapter.get_settings_value('url')
        return settings_url

    def _get_post_data(self):
        res = super(PrestashopWebhook, self)._get_post_data()
        return res.get('order', dict())

    def _prepare_workflow_data(self):
        post_data = self._get_post_data()
        vals = {
            'integration_workflow_states': [post_data['current_state']],
            'payment_method': post_data['payment'],
        }
        return vals

    def _check_webhook_digital_sign(self, adapter):
        return True  # TODO

    def _get_hook_name_method(self):
        headers = self._get_headers()
        header_name = self._get_hook_name_header()
        return headers[header_name]

    @staticmethod
    def _new_order_method_name():
        return 'actionValidateOrder'

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
