# See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import UserError


RUN_AFTER_PREFIX = 'run_after_'
RUN_BEFORE_PREFIX = 'run_before_'


class QuickConfiguration(models.AbstractModel):
    _name = 'configuration.wizard'
    _description = 'Quick Configuration'
    _steps = [
        ('step_finish', 'Finish'),
        ('step_languages', 'Step 2. Languages Mapping')
    ]

    integration_id = fields.Many2one(
        comodel_name='sale.integration',
        string='Sale Integration',
        ondelete='cascade',
    )

    state = fields.Char(default='step_finish')

    state_name = fields.Char(compute='_compute_state_index_and_visibility')
    state_index = fields.Integer(compute='_compute_state_index_and_visibility')
    show_previous = fields.Boolean(compute='_compute_state_index_and_visibility')
    show_next = fields.Boolean(compute='_compute_state_index_and_visibility')
    show_finish = fields.Boolean(compute='_compute_state_index_and_visibility')

    language_mapping_ids = fields.One2many(
        comodel_name='integration.res.lang.mapping',
        compute='_compute_language_mapping_ids',
        string='Languages Mapping',
    )

    language_default_id = fields.Many2one(
        comodel_name='integration.res.lang.external',
        string='Default Language',
        domain='[("integration_id", "=", integration_id)]'
    )
    start_initial_import = fields.Boolean(
        string='Start Initial Import',
        help='Start Initial Import of Master Data after clicking "Finish"',
    )

    def _compute_language_mapping_ids(self):
        self.language_mapping_ids = self.env['integration.res.lang.mapping'].search([
            ('integration_id', '=', self.integration_id.id)
        ])

    @api.depends('state')
    def _compute_state_index_and_visibility(self):
        for conf in self:
            steps_count = len(self._steps)

            conf.state_name = dict(self._steps).get(conf.state)
            conf.state_index = self._steps.index((conf.state, conf.state_name))
            conf.show_previous = conf.state_index != 0
            conf.show_next = conf.state_index + 1 != steps_count
            conf.show_finish = conf.state_index + 1 == steps_count

    @staticmethod
    def get_form_xml_id():
        raise NotImplementedError

    def get_action_view(self):
        self.ensure_one()
        view_xml_id = self.get_form_xml_id()
        view = self.env.ref(view_xml_id)

        return {
            'type': 'ir.actions.act_window',
            'name': 'Quick Configuration',
            'view_mode': 'form',
            'view_id': view.id,
            'res_model': self._name,
            'res_id': self.id,
            'target': 'new',
            'context': self.env.context,
        }

    def run_step_action(self, prefix):
        if not self.state:
            return False

        try:
            return getattr(self, prefix + self.state)()
        except AttributeError:
            return True

    def action_next_step(self):
        if self.run_step_action(RUN_AFTER_PREFIX):
            self.state = self._steps[self.state_index + 1][0]
            self.run_step_action(RUN_BEFORE_PREFIX)

        return self.get_action_view()

    def action_previous_step(self):
        self.state = self._steps[self.state_index - 1][0]
        self.run_step_action(RUN_BEFORE_PREFIX)

        return self.get_action_view()

    def action_finish(self):
        if self.start_initial_import:
            self.integration_id.integrationApiImportData()
            return self.env.ref('queue_job.action_queue_job').read()[0]
        return self.open_integration_view()

    def init_configuration(self):
        if self.integration_id.state == 'draft' or self.state == 'step_finish':
            self.state = self._steps[0][0]

        self.run_step_action(RUN_BEFORE_PREFIX)

    # Step Finish
    def run_before_step_finish(self):
        pass

    def run_after_step_finish(self):
        pass

    # Step Languages
    def run_before_step_languages(self):
        self.integration_id.integrationApiImportLanguages()

        lang_domain = [('integration_id', '=', self.integration_id.id)]

        language_code = self.integration_id.get_settings_value('language_id')

        if language_code:
            lang_domain += [('code', '=', language_code)]

        language_ids = self.env['integration.res.lang.external'].search(lang_domain)

        if len(language_ids) == 1:
            self.language_default_id = language_ids

    def run_after_step_languages(self):
        if self.language_mapping_ids.filtered(lambda x: not x.language_id):
            raise UserError(_('You should map all languages to continue'))

        if not self.language_default_id:
            raise UserError(_('You should select default language'))

        self.integration_id.set_settings_value('language_id', self.language_default_id.code)

        return True

    def action_go_to_languages(self):
        return self.env.ref('base.res_lang_act_window').read()[0]

    def action_eraze(self):
        self.ensure_one()
        wizards_to_unlink = self.search([
            ('integration_id', '=', self.integration_id.id),
        ])
        wizards_to_unlink.unlink()

    def open_integration_view(self):
        return {
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': self.integration_id._name,
            'res_id': self.integration_id.id,
            'context': self.env.context,
            'target': 'current',
        }


class QuickConfigurationTaxGroupAbstract(models.AbstractModel):
    _name = 'configuration.wizard.tax.group.abstract'
    _description = 'Quick Configuration Tax Group Abstact'
    _order = 'sequence, id'

    sequence = fields.Integer(
        string='Priority',
    )
    configuration_wizard_id = fields.Many2one(
        comodel_name='configuration.wizard',
        ondelete='cascade',
    )
    external_tax_group_id = fields.Many2one(
        comodel_name='integration.account.tax.group.external',
        string='External Tax Rule',
        readonly=True,
    )
    external_tax_ids = fields.Many2many(
        comodel_name='integration.account.tax.external',
        related='external_tax_group_id.external_tax_ids',
        string='Related Taxes',
        readonly=True,
    )
    default_external_tax_id = fields.Many2one(
        comodel_name='integration.account.tax.external',
        string='Default External Tax',
    )
