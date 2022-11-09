# See LICENSE file for full copyright and licensing details.

import logging
from odoo import fields, models, api

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _name = 'res.partner'
    _inherit = ['res.partner', 'integration.model.mixin']

    location_ids = fields.Many2many(
        'stock.location',
        'res_patner_location_relation',
        'partner_id',
        'location_id',
        string='Customer\'s locations'
    )
    is_address = fields.Boolean(
        string='Is Address',
        default=False,
    )

    integration_id = fields.Many2one(
        string='e-Commerce Integration',
        comodel_name='sale.integration',
        required=False,
        ondelete='set null',
    )

    @api.model
    def _commercial_fields(self):
        return super(ResPartner, self)._commercial_fields() + \
            ['integration_id']
