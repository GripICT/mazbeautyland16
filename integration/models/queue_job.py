#  Copyright 2020 VentorTech OU
#  License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import re
from odoo import models, fields, api, _
from odoo.addons.queue_job.job import FAILED
from odoo.exceptions import UserError


class QueueJob(models.Model):
    _inherit = 'queue.job'

    integration_exception_name = fields.Selection(
        selection=[
            (
                "NotMappedFromExternal",
                "e-Commerce System object not mapped with Odoo. Try to import external records "
                "from e-Commerce System, map it by hands or import into Odoo."
            ),
            (
                "NotMappedToExternal",
                "Odoo object not mapped with e-Commerce System. Try to import external records "
                "from e-Commerce System and map it."
            ),
            (
                "NoExternal",
                "External record doesn't exist. Try to import external records "
                "from e-Commerce System."
            ),
        ],
        string="Exception Name",
    )
    integration_model_name = fields.Text(string="Integration Model Name")
    integration_key = fields.Text(string="External or Internal Key")
    integration_model_view_name = fields.Text(
        string="Model Name",
        compute="_compute_view_name",
    )
    integration_id = fields.Many2one(comodel_name='sale.integration', string="Sale Integration")
    integration_external_id = fields.Text(string="External ID", compute="_compute_ids")
    integration_odoo_id = fields.Integer(string="Odoo ID", compute="_compute_ids")
    integration_external_name = fields.Text(string="External Name", compute="_compute_names")
    integration_odoo_name = fields.Text(string="Odoo Name", compute="_compute_names")

    def write(self, vals):
        result = super(QueueJob, self).write(vals)

        if vals.get("state") == "failed":
            exception_names = '|'.join([
                'NotMappedFromExternal',
                'NotMappedToExternal',
                'NoExternal'
            ])

            for job in self:
                integration_exception_name = None
                integration_model_name = None
                integration_key = None
                integration_id = None

                if job.exc_info:
                    match = re.search(
                        r'(%s): (.*)\((code|id)=(.*), integration=(.*)\)' % exception_names,
                        job.exc_info
                    )

                    if match:
                        integration_exception_name = match.group(1)
                        integration_model_name = match.group(2)
                        integration_key = match.group(4)

                        integration_id = match.group(5)

                        if integration_id:
                            integration_id = self.env['sale.integration'].browse(
                                int(integration_id)
                            )

                job.integration_exception_name = integration_exception_name
                job.integration_model_name = integration_model_name
                job.integration_key = integration_key
                job.integration_id = integration_id
        return result

    @api.model
    def requeue_integration_jobs(self, exception_name, model_name, key):
        jobs = self.sudo().search([
            ('state', '=', FAILED),
            ('integration_exception_name', '=', exception_name),
            ('integration_model_name', '=', model_name),
            ('integration_key', '=', key),
        ])

        if jobs:
            jobs.sudo().requeue()

    def get_model_from_integration_model_name(self):
        if not self.integration_model_name:
            return ''

        match = re.search(r'integration.(.*)(.mapping|.external)', self.integration_model_name)

        if match:
            return match.group(1)
        else:
            return ''

    def _compute_view_name(self):
        for job in self:
            model = job.get_model_from_integration_model_name()

            try:
                job.integration_model_view_name = self.env[model]._description
            except KeyError:
                job.integration_model_view_name = None

    def _compute_ids(self):
        for job in self:
            external_id = None
            odoo_id = None

            if job.integration_key:
                if job.integration_exception_name == 'NotMappedToExternal':
                    odoo_id = int(job.integration_key)
                else:
                    external_id = job.integration_key

            job.integration_external_id = external_id
            job.integration_odoo_id = odoo_id

    def _compute_names(self):
        for job in self:
            external_name = None
            odoo_name = None

            model = job.get_model_from_integration_model_name()

            if model and job.integration_exception_name:
                try:
                    if job.integration_exception_name == 'NotMappedToExternal':
                        odoo_name = self.env[model].search(
                            [('id', '=', job.integration_odoo_id)]
                        ).name
                    elif job.integration_id:
                        external_model = 'integration.%s.external' % model
                        external_name = self.env[external_model].search([
                            ('code', '=', job.integration_external_id),
                            ('integration_id', '=', job.integration_id.id),
                        ]).name
                except KeyError:
                    pass

            job.integration_external_name = external_name
            job.integration_odoo_name = odoo_name

    def action_open_mapping_view(self):
        model = self.get_model_from_integration_model_name()

        if model:
            action_name = 'integration.integration_%s_mapping_action' % model.replace('.', '_')

            return self.env.ref(action_name).read()[0]

    def action_open_external_view(self):
        model = self.get_model_from_integration_model_name()

        if model:
            action_name = 'integration.integration_%s_external_action' % model.replace('.', '_')

            return self.env.ref(action_name).read()[0]

    def action_import_from_external_system(self):
        model = self.get_model_from_integration_model_name()
        external_model = 'integration.%s.external' % model

        external_old = self.env[external_model].search([
            ('integration_id', '=', self.integration_id.id)
        ])

        if model == 'product.attribute.value':
            self.integration_id.integrationApiImportAttributeValues()
        elif model == 'res.country':
            self.integration_id.integrationApiImportCountries()
        elif model == 'account.tax':
            self.integration_id.integrationApiImportTaxes()
        elif model == 'res.lang':
            self.integration_id.integrationApiImportLanguages()
        elif model == 'sale.order.payment.method':
            self.integration_id.integrationApiImportPaymentMethods()
        elif model == 'product.product':
            self.integration_id.integrationApiImportProductsTemplate()
        elif model == 'product.public.category':
            self.integration_id.integrationApiImportCategories()
        elif model == 'product.template':
            self.integration_id.integrationApiImportProductsTemplate()
        elif model == 'delivery.carrier':
            self.integration_id.integrationApiImportDeliveryMethods()
        elif model == 'res.country.state':
            self.integration_id.integrationApiImportStates()
        elif model == 'account.tax.group':
            self.integration_id.integrationApiImportAccountTaxGroups()
        elif model == 'sale.order.sub.status':
            self.integration_id.integrationApiImportSaleOrderStatuses()
        elif model == 'product.attribute':
            self.integration_id.integrationApiImportAttributes()
        else:
            raise UserError(_('Can`t run import for model "%s"') % model)

        external_new = self.env[external_model].search([
            ('integration_id', '=', self.integration_id.id)
        ])

        external_delta = external_new - external_old
        message = ''

        if external_old:
            message = _('Updated records: ') + str(len(external_old))

        if external_delta:
            if message:
                message += '\n\n'

            message += _('Imported new external records: ') + str(len(external_delta))
            for external in external_delta:
                message += '\n\t%s\t%s' % (str(external.code), external.name)

        message_id = self.env['message.wizard'].create({'message': message})

        return {
            'name': _('Import list of %s') % self.integration_model_view_name,
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': 'message.wizard',
            'res_id': message_id.id,
            'target': 'new'
        }
