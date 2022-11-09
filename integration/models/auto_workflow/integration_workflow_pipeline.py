# See LICENSE file for full copyright and licensing details.

import logging

from odoo import models, fields, _
from odoo.addons.integration.tools import raise_requeue_job_on_concurrent_update
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)

SKIP = 'skip'
TO_DO = 'todo'
DONE = 'done'
FAILED = 'failed'
IN_PROCESS = 'in_process'

PIPELINE_STATE = [
    (SKIP, 'Skip'),
    (TO_DO, 'ToDo'),
    (IN_PROCESS, 'In Process'),
    (FAILED, 'Failed'),
    (DONE, 'Done'),
]


class IntegrationWorkflowPipelineLine(models.Model):

    _name = 'integration.workflow.pipeline.line'
    _description = 'Integration Workflow Pipeline Line'

    name = fields.Char(
        string='Name',
        compute='_compute_name',
    )
    state = fields.Selection(
        selection=PIPELINE_STATE,
        string='State',
        default=TO_DO,
    )
    current_step_method = fields.Char(
        string='Current Step',
        required=True,
    )
    next_step_method = fields.Char(
        string='Next Step',
    )
    order_id = fields.Many2one(
        comodel_name='sale.order',
        related='pipeline_id.order_id',
    )
    integration_id = fields.Many2one(
        comodel_name='sale.integration',
        related='pipeline_id.order_id.integration_id',
    )
    pipeline_id = fields.Many2one(
        comodel_name='integration.workflow.pipeline',
        string='Pipeline',
        ondelete='cascade',
    )

    def _compute_name(self):
        for rec in self:
            rec.name = ' '.join([x.capitalize() for x in rec.current_step_method.split('_')])

    def run(self):
        if self.state in (SKIP, DONE):
            raise UserError(_('Inactive task for the current workflow!'))

        self._validate_previous()

        order_method_name = f'_integration_{self.current_step_method}'
        order_method = getattr(self.order_id, order_method_name)

        result, __ = order_method()

        self.state = DONE if result is True else FAILED
        return self.pipeline_id.open_form()

    def run_with_delay(self):
        if self.state not in (SKIP, DONE):
            self.state = IN_PROCESS

        job_kwargs = {
            'channel': self.env.ref('integration.channel_sale_order').complete_name,
            'identity_key': f'integartion_pipeline_task-{self.integration_id.id}-{self}',
            'description': 'Integartion Workflow Line: '
            + f'[{self.integration_id.id}][{self.order_id.display_name}] {self.name}',
        }
        return self.with_delay(**job_kwargs)._run_and_call_next()

    @raise_requeue_job_on_concurrent_update
    def _run_and_call_next(self):
        if self.state in (SKIP, DONE):
            self.pipeline_id._call_next_step(self.next_step_method)
            return _('Task was skipped.')

        order_method_name = f'_integration_{self.current_step_method}'
        order_method = getattr(self.order_id, order_method_name)

        result, message = order_method()

        if result is True:
            self.state = DONE
            self.pipeline_id._call_next_step(self.next_step_method)
            return message

        self.state = FAILED
        return result

    def _validate_previous(self):
        states = self.pipeline_id.pipeline_task_ids\
            .filtered(lambda x: x.id < self.id and x.state != SKIP).mapped('state')

        if states and not all(x == DONE for x in states):
            raise UserError(
                _('Not the all previous tasks in the state DONE. Fix them first.')
            )


class IntegrationWorkflowPipeline(models.Model):

    _name = 'integration.workflow.pipeline'
    _description = 'Integration Workflow Pipeline'

    order_id = fields.Many2one(
        comodel_name='sale.order',
        string='Order',
        ondelete='cascade',
        required=True,
    )
    input_file_id = fields.Many2one(
        comodel_name='sale.integration.input.file',
        string='Input File',
    )
    sub_state_external_ids = fields.Many2many(
        comodel_name='integration.sale.order.sub.status.external',
        relation='pipeline_external_sub_state_relation',
        column1='pipeline_id',
        column2='sub_state_external_id',
        string='External Sub Status Code',
    )
    invoice_journal_id = fields.Many2one(
        comodel_name='account.journal',
        compute='_compute_invoice_journal',
        string='Invoice Journal',
    )
    force_invoice_date = fields.Boolean(
        string='Force Invoice Date',
    )
    payment_method_external_id = fields.Many2one(
        comodel_name='integration.sale.order.payment.method.external',
        string='External Payment Method',
    )
    payment_journal_id = fields.Many2one(
        comodel_name='account.journal',
        related='payment_method_external_id.payment_journal_id',
    )
    pipeline_task_ids = fields.One2many(
        comodel_name='integration.workflow.pipeline.line',
        inverse_name='pipeline_id',
        string='Pipeline Tasks',
    )

    def _compute_invoice_journal(self):
        for rec in self:
            invoice_journals = self.sub_state_external_ids\
                .mapped('invoice_journal_id')
            rec.invoice_journal_id = (invoice_journals[:1]).id

    def manual_run(self):
        self.ensure_one()
        job_kwargs = self.order_id._build_workflow_job_kwargs()
        self.with_delay(**job_kwargs).trigger_pipeline()
        return self.open_form()

    def drop_pipeline(self):
        return self.unlink()

    def trigger_pipeline(self):
        _logger.info(
            'Trigger pipeline [%s] %s: %s', self.order_id.display_name, self, self._tasks_info()
        )
        task_to_run = self.pipeline_task_ids\
            .filtered(lambda x: x.state != DONE)[:1]

        if not task_to_run:
            return _(
                'There are no active tasks for the current pipeline: %s' % self._tasks_info()
            )

        return task_to_run.run_with_delay()

    def _call_next_step(self, next_step_name):
        task_to_run = self.pipeline_task_ids\
            .filtered(lambda x: x.current_step_method == next_step_name)

        if not task_to_run:
            return _('Workflow Done!')

        return task_to_run.run_with_delay()

    def _tasks_info(self):
        return [(x.name, x.state) for x in self.pipeline_task_ids]

    def open_form(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Integration Workflow',
            'res_model': self._name,
            'view_mode': 'form',
            'view_id': self.env.ref('integration.integration_workflow_pipeline_form_view').id,
            'res_id': self.id,
            'target': 'new',
        }
