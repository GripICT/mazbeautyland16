# See LICENSE file for full copyright and licensing details.

from odoo import models


class DeliveryCarrier(models.Model):
    _name = 'delivery.carrier'
    _inherit = ['delivery.carrier', 'integration.model.mixin']
    _internal_reference_field = 'name'
