# -*- coding: utf-8 -*-
"""
smart_reorder_advisor.utils.access
====================================
Lightweight declarative authorization helper.

Usage::

    from ..utils.access import require_group

    class MyModel(models.Model):
        @require_group('smart_reorder_advisor.group_smart_reorder_manager')
        def action_do_something(self):
            ...

The decorator wraps the method and raises odoo.exceptions.AccessError before
the body executes if the calling user is not a member of the specified group.
Using AccessError (not UserError) keeps the semantics honest: the operation is
refused because of *who you are*, not because of *what you asked for*.

Why a decorator instead of a first-line has_group() check?
  - The gating intent is visible at the method signature, not buried inside.
  - Adding the same guard to a new action is one line, not three.
  - A future audit (e.g. security review) can grep for @require_group across the
    whole module and instantly see every manager-only surface.
"""
import functools

from odoo.exceptions import AccessError
from odoo import _


def require_group(xml_id: str):
    """
    Method decorator that raises AccessError if self.env.user is not a member
    of the Odoo security group identified by *xml_id*.

    :param xml_id: Fully-qualified XML ID of the group, e.g.
                   ``'smart_reorder_advisor.group_smart_reorder_manager'``
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            if not self.env.user.has_group(xml_id):
                group = self.env.ref(xml_id, raise_if_not_found=False)
                group_name = group.name if group else xml_id
                raise AccessError(
                    _('This action requires membership in the "%s" group.') % group_name
                )
            return fn(self, *args, **kwargs)
        return wrapper
    return decorator
