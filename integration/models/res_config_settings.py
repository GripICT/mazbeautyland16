# See LICENSE file for full copyright and licensing details.

import binascii
import os


from odoo import fields, models

# API keys support
API_KEY_SIZE = 20  # in bytes


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    integration_api_key = fields.Char(
        string='API Key',
        compute='_compute_integration_api_key',
        help='API key for the integration.',
    )

    def _compute_integration_api_key(self):
        """ Compute API key for the installed integration. """
        for record in self:
            record.integration_api_key = self.get_integration_api_key()

    def generate_integration_api_key(self):
        """ Generate API key for the installed integration. """
        api_key = binascii.hexlify(os.urandom(API_KEY_SIZE)).decode()
        self.env['ir.config_parameter'].sudo().set_param('integration.integration_api_key', api_key)
        return self._compute_integration_api_key()

    def get_values(self):
        """ Get values for the installed integration. """
        res = super(ResConfigSettings, self).get_values()

        integration_api_key = self.get_integration_api_key()
        res.update(integration_api_key=integration_api_key)

        return res

    def get_integration_api_key(self):
        """ Get API key for the installed integration. """
        return self.env['ir.config_parameter'].sudo().get_param('integration.integration_api_key')
