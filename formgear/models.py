# -*- coding: utf-8 -*-
#
from __future__ import print_function
import os
import yaml
import mongo
from functools import partial
import types

from formgear.fields import FieldsRegistry
from formgear.widgets import WidgetRegistry
from registry import Registry
from formgear.exceptions import *
from formgear.utils import yamls_files, file_resolve

from jinja2 import Environment, PackageLoader

__author__ = 'xen'

yamlsfiles = yamls_files()


class ModelRegistry(Registry):
    NotFound = NotFoundModelException

class FormWrap(object):
    def __init__(self, forms, model):
        self.model = model

        self.forms = forms

    def get(self, name):
        name = name or 'default'
        for form in self.forms:
            if form['name'] == name:
                    return form

        raise KeyError("Form %r not found for model %r" % (name, self.model))

    def field(self, name):
        return self.model._fields_dict[name]

    def __call__(self, name, fields=[]):
        if not fields:
            form = self.get(name)
            if form:
                fields = form['fields']

        ret = [
                (name, self.field(name))
                for name in fields
        ]
        return ret


    def __getattribute__(self, name):
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            if self.get(name) is None:
                raise

        if isinstance(self.model, type):
            return partial(self.model, subform=name)

        obj = self.model.__class__(subform=name)
        obj._fields = self.model._fields
        return obj


class MetaModel(type):
    """
    Base model metaclass
    """
    def __new__(cls, name, bases, attrs):
        meta = attrs.pop('Meta', None)
        abstract = getattr(meta, 'abstract', False)
        registername = attrs.pop('name', name.lower())

        cfg = {}
        # we have search __yaml__ attribute only, when we
        # have initialize a subclass of formgear.models.Model
        if not abstract:

            # try to find out by __yaml__ or by class name
            # __yaml__ = "order" or class Order(Models):
            ypath = attrs.get('__yaml__') or name.lower()

            ypath = yamlsfiles.get(ypath, ypath)
            if not os.access(ypath, 0):
                raise YamlEntryNotFoundInListException

            cfg = yaml.safe_load(open(ypath))

            attrs["__yaml__"] = ypath

        # this block should make model registry list looks better
        _descr = ''
        if cfg.get('description', None):
            _descr = cfg.get('description')
        if attrs.get('__doc__', None) and len(attrs.get('__doc__')):
            _descr = attrs.get('__doc__')
        else:
            attrs['__doc__'] = _descr
        _title = registername
        if cfg.get('title', None):
            _title = cfg.get('title')

        cfg['_name'] = registername
        cfg['_descr'] = _descr
        cfg['_title'] = _title

        fields = []
        for field in cfg.pop('fields', []):
            if 'name' not in field:
                raise ParsingException("Oops, we found nameless field!")

            field_typ = field.pop('type', 'string')
            field_class = FieldsRegistry.resolve(field_typ)

            # extracts widget information
            widget = field.pop('widget', field_class.widget)

            if isinstance(widget, dict):
                widget_typ = widget.pop('type', field_class.widget)
                widget_kw = widget
            else:
                widget_typ = widget
                widget_kw = {}

            if isinstance(widget_typ, basestring):
                widgt_class = WidgetRegistry.resolve(widget_typ)
            else:
                widgt_class = widget_typ

            wdgt = widgt_class(**widget_kw)

            file_resolve(field, "choices", ypath)

            new_field = field_class(widget = wdgt, **field)

            # actual work with fields
            # XXX: here is missed part with validators
            fields.append((field.pop('name'), new_field))

        forms = []
        forms.extend(cfg.pop('forms', []))
        forms.append({
            "name": "default",
            "fields": [
                fname
                for fname, _field in fields
                if hasattr(_field, 'title')
            ],
        })

        cfg.update(attrs)
        newbornclass = super(MetaModel, cls).__new__(cls, name, bases, cfg)

        for fname, ffunc in fields:
            setattr(newbornclass, fname, ffunc)

        newbornclass._fields = fields
        newbornclass._fields_dict = dict(fields)


        newbornclass.form = FormWrap(forms, newbornclass)

        if not abstract:
            #print("Register widget:", registername)
            ModelRegistry.register(newbornclass, registername)

        return newbornclass

class Model(object):
    __metaclass__ = MetaModel
    subform = None

    class Meta:
        abstract = True

    def __init__(self, data=None, subform=None, _id=None, _raw=False, **kw):
        assert data is None or not kw, 'Pass data in one way'
        if data:
            kw = data
        if _id:
            self._id = _id

        self.subform = subform
        form = self.form.get(subform)
        self.render_form = self._render_form

        fields = []
        for name, _field in self._fields:
            if name not in form['fields']:
                continue

            field = _field.reinstance()
            fields.append((name, field))

        self._fields = fields
        self._fields_dict = dict(fields)
        self.form = FormWrap(self.form.forms, self)

        self.update(kw, raw=_raw)
        if _id:
            self.lock_id()

    def update(self, data=None, raw=False, **kw):
        assert data is None or not kw, 'Pass data in one way'
        kw = data or kw
        if callable(getattr(kw, 'items', None)):
            kw = kw.items()

        for name, val in kw:

            field = self._field(name)
            if not field:
                continue

            if getattr(field, 'locked', False):
                continue

            field.value = val

    def items(self):
        for name, field in self._fields:
            yield name, field.value

    def __iter__(self):
        return iter(self.form())

    def _field(self, name):
        return self._fields_dict.get(name)

    def __getattribute__(self, name):
        try:
            fields = object.__getattribute__(self,'_fields_dict')
            if name in fields:
                return self._field(name).value

        except AttributeError:
            pass

        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        try:
            fields = object.__getattribute__(self,'_fields_dict')
            if name in fields:
                self._field(name).value = value
                return

        except AttributeError:
            pass

        return object.__setattr__(self, name, value)

    def validate(self):
        for name, field in self._fields:
            if not hasattr(field, 'validate'):
                continue

            valid = field.validate()
            if not valid:
                return

        return True

    def to_mongo(self):

        doc = dict([
            (name, field.to_mongo)
            for name,field in self._fields
        ])

        if '_id' in doc:
            pass
        elif hasattr(self, 'key'):
            _id = self.key()
            if not (_id is None):
                doc['_id'] = _id
        elif hasattr(self, '_id'):
           doc['_id'] = self._id

        return doc

    def lock_id(self):
        if not hasattr(self.__class__, '__key__'):
            return

        if not isinstance(self.__key__, (list, tuple)):
            return

        for name in self.__key__:
            field = self._field(name)
            if not field:
                continue

            field.locked = True


    def key(self):
        if not hasattr(self.__class__, '__key__'):
            return

        if isinstance(self.__key__, (list, tuple)):
            if self.__key__[0] == '_id':

                # don`t generate random id twice
                if hasattr(self, '_id'):
                    return self._id

                import bson
                names = self.__key__[1:]
                vals = [
                        unicode(bson.objectid.ObjectId())
                ]
            else:
                names = self.__key__
                vals = []

            vals.extend([
                getattr(self, fieldname)
                for fieldname in names
            ])
            assert None not in vals, "Field must have a value \
if specified in __key__"

            return unicode.join(u"::", vals)

        elif isinstance(self.__key__, basestring):
            return getattr(self, self.__key__)
        elif callable(self.__key__):
            return self.__key__()
        elif hasattr(self.__class__.__key__, 'getter'):
            return self.__key__

        assert False, "Who is Mr. __key__?"

    def save(self):
        _id = getattr(self, '_id', None)
        self._id = mongo.save(self.kind(), self.to_mongo(), _id)
        return self._id

    @classmethod
    def all(cls, **kw):
        return [
                cls(_raw=True, **data)
                for data in 
                mongo.find(cls.kind(), **kw)
        ]

    @classmethod
    def kind(cls):
        return cls.__name__.lower()

    @classmethod
    def count(cls):
        return cls.all().count()

    @classmethod
    def get(cls, key=None, **kw):
        if not kw and key:
            kw = {"_id": key}

        data = list(cls.all(**kw)[:1])
        if not data:
            return

        return data[0]

    @classmethod
    def delete(cls, _filter):
        mongo.remove(cls.kind(), _filter)

    def render_form(self, env=None, state='edit', form=None, **kw):
        """ Render form method
        """
        assert form is None or self.subform is None
        env = env or Environment(loader=PackageLoader('formgear'))
        template = env.get_template('form.html')
        m = getattr(template.module, state, None)

        fields = self.form(form or self.subform or 'default')

        return m(form = fields, **kw)

    _render_form = render_form
    render_form.environmentfunction = True
    render_form = classmethod(render_form)
