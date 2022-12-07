# See LICENSE file for full copyright and licensing details.

import logging

from odoo import models, fields, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.integration.tools import raise_requeue_job_on_concurrent_update


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
    skip_dispatch = fields.Boolean(
        related='pipeline_id.skip_dispatch',
    )

    def _compute_name(self):
        for rec in self:
            rec.name = ' '.join([x.capitalize() for x in rec.current_step_method.split('_')])

    def run(self):
        if self.state in (SKIP, DONE):
            raise UserError(_('Inactive task for the current workflow!'))

        self._validate_previous()

        order_method = self._retrieve_current_order_method()
        result, message = order_method()

        if result is False:
            self._failed_job_manually(message)

        self.state = DONE if result is True else FAILED
        return self.pipeline_id.open_form()

    def run_with_delay(self):
        if self.state not in (SKIP, DONE):
            self.state = IN_PROCESS

        job_kwargs = self._build_task_job_kwargs()

        return self.with_delay(**job_kwargs)._run_and_call_next()

    def _failed_job_manually(self, message):
        job_kwargs = self._build_task_job_kwargs()
        return self.with_delay(**job_kwargs)._raise_job(message)

    def _build_task_job_kwargs(self):
        return {
            'channel': self.env.ref('integration.channel_sale_order').complete_name,
            'identity_key': f'integartion_pipeline_task-{self.integration_id.id}-{self}',
            'description': 'Integartion Workflow Line: '
            + f'[{self.integration_id.id}] [{self.order_id.display_name}] {self.name}',
        }

    def _raise_job(self, message):
        raise ValidationError(message)

    @raise_requeue_job_on_concurrent_update
    def _run_and_call_next(self):
        if self.state in (SKIP, DONE):
            self.pipeline_id._call_pipeline_step(self.next_step_method)
            return _('Task was skipped.')

        order_method = self._retrieve_current_order_method()
        result, message = order_method()

        if result is True:
            self.state = DONE
            self.pipeline_id._call_pipeline_step(self.next_step_method)
            return message

        if result is False:
            self._failed_job_manually(message)

        self.state = FAILED
        return result

    def _retrieve_current_order_method(self):
        order = self.order_id
        if self.skip_dispatch:
            order = order.with_context(skip_dispatch_to_external=True)
        return getattr(order, f'_integration_{self.current_step_method}')

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
    skip_dispatch = fields.Boolean(
        string='Skip Dispatch',
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

    def _call_pipeline_step(self, step_name):
        task_to_run = self.pipeline_task_ids\
            .filtered(lambda x: x.current_step_method == step_name)

        if not task_to_run:
            return _('Workflow Done!')

        return task_to_run.run_with_delay()

    def _tasks_info(self):
        return [(x.name, x.state) for x in self.pipeline_task_ids]

    def _update_and_re_run_pipeline(self, order_data):
        _logger.info('Update existing order pipeline %s', self)

        order = self.order_id
        task_list, pipeline_vals = order._build_task_list_and_vals(order_data)
        sub_state_ids = pipeline_vals['sub_state_external_ids'][0][-1]
        self.write({
            'sub_state_external_ids': [(4, x, 0) for x in sub_state_ids],
            'skip_dispatch': self._context.get('default_skip_dispatch', False),
        })

        pipeline_task_ids = self.pipeline_task_ids

        for task_name, task_enable in task_list:
            task = pipeline_task_ids.filtered(
                lambda x: x.current_step_method == task_name
            )
            if task and task.state != DONE and task_enable:
                task.state = TO_DO
                _logger.info('Pipeline task "%s" was marked as "%s".', task_name, TO_DO)

        job_kwargs = order._build_workflow_job_kwargs()
        return self.with_delay(**job_kwargs).trigger_pipeline()

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
