import bpy

from .. import registry
from ..version_utils import assign_props, IS_28


_collaps_op_props = {
    'key': bpy.props.StringProperty(),
}


class _CollapsOp(bpy.types.Operator):
    bl_idname = 'io_scene_xray.collaps'
    bl_label = ''
    bl_description = 'Show / hide UI block'

    if not IS_28:
        for prop_name, prop_value in _collaps_op_props.items():
            exec('{0} = _collaps_op_props.get("{0}")'.format(prop_name))

    _DATA = {}

    @classmethod
    def get(cls, key):
        return cls._DATA.get(key, False)

    def execute(self, _context):
        _CollapsOp._DATA[self.key] = not _CollapsOp.get(self.key)
        return {'FINISHED'}


assign_props([
    (_collaps_op_props, _CollapsOp),
])


def draw(layout, key, text=None, enabled=None, icon=None, style=None):
    col = layout.column(align=True)
    row = col.row(align=True)
    if (enabled is not None) and (not enabled):
        row_operator = row.row(align=True)
        row_operator.enabled = False
    else:
        row_operator = row
    isshow = _CollapsOp.get(key)
    if icon is None:
        icon = 'TRIA_DOWN' if isshow else 'TRIA_RIGHT'
    kwargs = {}
    if text is not None:
        kwargs['text'] = text
    box = None
    if isshow:
        box = col
        if style != 'nobox':
            box = box.box()
    if style == 'tree':
        row.alignment = 'LEFT'
        row = row_operator.row()
        if box:
            bxr = box.row(align=True)
            bxr.alignment = 'LEFT'
            bxr.label(text='')
            box = bxr.column()
    oper = row_operator.operator(_CollapsOp.bl_idname, icon=icon, emboss=style != 'tree', **kwargs)
    oper.key = key
    return row, box


def is_opened(key):
    return _CollapsOp.get(key)


registry.module_requires(__name__, [
    _CollapsOp,
])
