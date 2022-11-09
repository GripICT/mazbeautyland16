# See LICENSE file for full copyright and licensing details.

import base64
from psycopg2 import OperationalError
from itertools import groupby
from functools import wraps
from operator import attrgetter
from collections import namedtuple, defaultdict, OrderedDict

from cerberus import Validator

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.service.model import PG_CONCURRENCY_ERRORS_TO_RETRY
from odoo.tools.mimetypes import guess_mimetype
from odoo.exceptions import ValidationError
from odoo import _


IS_TRUE = '1'
IS_FALSE = '0'


def _guess_mimetype(data):
    if not data:
        return None

    raw = base64.b64decode(data)
    mimetype = guess_mimetype(raw)
    return mimetype


def not_implemented(method):
    def wrapper(self, *args, **kw):
        raise ValidationError(_(
            '[Debug] This feature is still not implemented (%s.%s()).'
            % (self.__class__.__name__, method.__name__)
        ))
    return wrapper


def raise_requeue_job_on_concurrent_update(method):
    @wraps(method)
    def wrapper(self, *args, **kw):
        try:
            result = method(self, *args, **kw)
            # flush() is needed to push all the pending updates to the database
            self.env["base"].flush()
            return result
        except OperationalError as e:
            if e.pgcode in PG_CONCURRENCY_ERRORS_TO_RETRY:
                raise RetryableJobError(str(e))
            else:
                raise

    return wrapper


class TemplateHub:
    """Validate products before import."""

    _schema = OrderedDict({
        'id': {'type': 'string', 'required': True},
        'barcode': {'type': 'string', 'required': True},
        'ref': {'type': 'string', 'required': True},
        'parent_id': {'type': 'string', 'required': True},
        'skip_ref': {'type': 'boolean', 'required': True},
    })

    def __init__(self, input_list):
        assert type(input_list) == list
        # Because it works very slow with big pack of data
        # self._validate_input(input_list)

        self.ptuple = namedtuple('Product', self._schema.keys())
        self.product_list = self._convert_to_clean(input_list)

    def __iter__(self):
        for rec in self.product_list:
            yield rec

    def get_empty_ref_ids(self):
        """
        :result: ([1, 2, 3], [4, 5, 6])
        """
        templates, variants = self._split_products(
            [x for x in self if not x.ref and not x.skip_ref]
        )
        return [self._format_rec(t) for t in templates], [self._format_rec(v) for v in variants]

    def get_dupl_refs(self):
        """
        :result: {'BAR': [1, 2], 'FOO': [1, 2, 3]}
        """
        products = [x for x in self if x.ref and not x.skip_ref]
        return self._group_by(products, 'ref', level=2)

    def get_dupl_barcodes(self):
        """
        :result: {'XX01': [1, 2], 'XX02': [1, 2, 3]}
        """
        products = [x for x in self if x.barcode]
        return self._group_by(products, 'barcode', level=2)

    @classmethod
    def from_odoo(cls, search_list):
        """Make class instance from odoo search."""
        def parse_args(rec):
            return {
                'id': str(rec['id']),
                'barcode': rec['barcode'] or str(),
                'ref': rec['default_code'] or str(),
                'parent_id': str(rec['product_tmpl_id'][0]),
                'skip_ref': False,
            }
        return cls([parse_args(rec) for rec in search_list])

    @classmethod
    def get_ref_intersection(cls, self_a, self_b):
        """Find references intersection of different instances."""
        def parse_ref(self_):
            return {x.ref for x in self_ if x.ref and not x.skip_ref}

        def filter_records(scope):
            return [x for x in self_a if x.ref in scope], [x for x in self_b if x.ref in scope]

        joint_ref = parse_ref(self_a) & parse_ref(self_b)
        records_a, records_b = filter_records(joint_ref)

        return self_a._group_by(records_a, 'ref'), self_b._group_by(records_b, 'ref')

    def _validate_input(self, input_list):
        frame = Validator(self._schema)
        for record in input_list:
            if not frame.validate(record):
                raise ValidationError(_(
                    'Invalid product serialization: %s' % str(record)
                ))

    def _convert_to_clean(self, input_list):
        """Convert to namedtuple for convenient handling."""
        return [self._serialize_by_scheme(rec) for rec in input_list]

    def _serialize_by_scheme(self, record):
        args_list = [record[key] for key in self._schema.keys()]
        return self.ptuple(*args_list)

    @staticmethod
    def _format_rec(rec):
        return f'{rec.parent_id} - {rec.id}' if rec.parent_id else rec.id

    @staticmethod
    def _split_products(records):
        templates = [x for x in records if not x.parent_id]
        variants = [x for x in records if x.parent_id]
        return templates, variants

    def _group_by(self, records, attr, level=False):
        dict_ = defaultdict(list)
        [
            [dict_[key].append(self._format_rec(x)) for x in grouper]
            for key, grouper in groupby(records, key=attrgetter(attr))
        ]
        if level:
            return {
                key: val for key, val in dict_.items() if len(val) >= level
            }
        return dict(dict_)


class HtmlWrapper:
    """Helper for html wrapping lists and dicts."""

    def __init__(self, integration, adapter=False):
        self.integration = integration
        self.adapter = adapter or integration._build_adapter()
        self.base_url = integration.sudo().env['ir.config_parameter'].get_param('web.base.url')
        self.html_list = list()

    @property
    def has_message(self):
        return bool(self.html_list)

    def dump(self):
        return '<br/>'.join(self.html_list)

    def add_title(self, title):
        self._extend_html_list(self._wrap_title(title))

    def add_subtitle(self, title):
        self._extend_html_list(self._wrap_subtitle(title))

    def add_sub_block_for_external_product_list(self, title, id_list):
        title = self._wrap_string(title)
        body = self._wrap_external_product_list(id_list)
        self._extend_html_list(title % body)

    def add_sub_block_for_external_product_dict(self, title, dct):
        title = self._wrap_string(title)
        body = self._format_external_product_dict(dct)
        self._extend_html_list(title % body)

    def add_sub_block_for_internal_template_list(self, title, id_list):
        title = self._wrap_string(title)
        body = self._wrap_internal_template_list(id_list)
        self._extend_html_list(title % body)

    def add_sub_block_for_internal_variant_list(self, title, id_list):
        title = self._wrap_string(title)
        body = self._wrap_internal_variant_list(id_list)
        self._extend_html_list(title % body)

    def add_sub_block_for_internal_template_dict(self, title, dct):
        title = self._wrap_string(title)
        body = self._format_internal_template_dict(dct)
        self._extend_html_list(title % body)

    def add_sub_block_for_internal_variant_dict(self, title, dct):
        title = self._wrap_string(title)
        body = self._format_internal_variant_dict(dct)
        self._extend_html_list(title % body)

    def add_sub_block_for_internal_custom_dict(self, title, dct, model_):
        title = self._wrap_string(title)
        body = self._format_internal_custom_dict(dct, model_)
        self._extend_html_list(title % body)

    def build_internal_link(self, id_, model_, name):
        return self._build_internal_link(id_, model_, name)

    def _format_internal_template_dict(self, dct):
        dct_ = self._cut_duplicates(dct)
        return ''.join([
            f'<li>{k}<ul>{self._wrap_internal_template_list(v)}</ul></li>' for k, v in dct_.items()
        ])

    def _format_internal_variant_dict(self, dct):
        dct_ = self._cut_duplicates(dct)
        return ''.join([
            f'<li>{k}<ul>{self._wrap_internal_variant_list(v)}</ul></li>' for k, v in dct_.items()
        ])

    def _format_internal_custom_dict(self, dct, model_):
        dct_ = self._cut_duplicates(dct)
        return ''.join([
            f'<li>{k}<ul>{self._wrap_internal_custom_list(v, model_)}</ul></li>'
            for k, v in dct_.items()
        ])

    def _format_external_product_dict(self, dct):
        dct_ = self._cut_duplicates(dct)
        return ''.join([
            f'<li>{k}<ul>{self._wrap_external_product_list(v)}</ul></li>' for k, v in dct_.items()
        ])

    def _wrap_internal_template_list(self, id_list):
        return self._convert_to_html('product.template', id_list)

    def _wrap_internal_variant_list(self, id_list):
        return self._convert_to_html('product.product', id_list)

    def _wrap_internal_custom_list(self, id_list, model_):
        return self._convert_to_html(model_, id_list)

    def _wrap_external_product_list(self, id_list):
        return self.adapter._convert_to_html(id_list)

    @staticmethod
    def _wrap_string(title):
        return f'<div>{title}<ul>%s</ul></div>'

    @staticmethod
    def _wrap_title(title):
        return f'<div><strong>{title}</strong><hr/></div>'

    @staticmethod
    def _wrap_subtitle(title):
        return f'<div>{title}<hr/></div>'

    @staticmethod
    def _cut_duplicates(dct):
        return {k: list(set(v)) for k, v in dct.items()}

    @staticmethod
    def _internal_pattern():
        return '<a href="%s/web#id=%s&model=%s&view_type=form" target="_blank">%s</a>'

    def _extend_html_list(self, html_text):
        self.html_list.append(html_text)

    def _convert_to_html(self, model_, id_list):
        arg_list = (([y.strip() for y in x.split('-')][-1], model_, x) for x in id_list)
        links = (self._build_internal_link(*args) for args in arg_list)
        return ''.join([f'<li>{link}</li>' for link in links])

    def _build_internal_link(self, id_, model_, name):
        pattern = self._internal_pattern()
        return pattern % (self.base_url, id_, model_, name)
