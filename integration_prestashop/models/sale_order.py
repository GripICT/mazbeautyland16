# See LICENSE file for full copyright and licensing details.

from odoo import models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def _prestashop_cancel_order(self):
        status = self.integration_id.sub_status_cancel_id
        self.sub_status_id = status

    def _prestashop_shipped_order(self):
        status = self.integration_id.sub_status_shipped_id
        self.sub_status_id = status
