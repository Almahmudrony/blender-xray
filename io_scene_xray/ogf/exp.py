from bmesh.ops import triangulate
import bpy
import mathutils

from ..xray_io import ChunkedWriter, PackedWriter
from .fmt import Chunks, ModelType, VertexFormat
from ..utils import is_exportable_bone, find_bone_exportable_parent, AppError, \
    fix_ensure_lookup_table, convert_object_to_space_bmesh, \
    calculate_mesh_bbox, gen_texture_name
from ..utils import is_helper_object, save_file
from ..xray_motions import MATRIX_BONE_INVERTED
from ..version_utils import multiply, IS_28


def calculate_mesh_bsphere(bbox, vertices, mat=mathutils.Matrix()):
    center = (bbox[0] + bbox[1]) / 2
    _delta = bbox[1] - bbox[0]
    max_radius = max(abs(_delta.x), abs(_delta.y), abs(_delta.z)) / 2
    for vtx in vertices:
        relative = multiply(mat, vtx.co) - center
        radius = relative.length
        if radius > max_radius:
            offset = center - relative.normalized() * max_radius
            center = (multiply(mat, vtx.co) + offset) / 2
            max_radius = (center - offset).length
    return center, max_radius


def calculate_bbox_and_bsphere(bpy_obj, apply_transforms=False, cache={}):
    def scan_meshes(bpy_obj, meshes):
        if is_helper_object(bpy_obj):
            return
        if (bpy_obj.type == 'MESH') and bpy_obj.data.vertices:
            meshes.append(bpy_obj)
        for child in bpy_obj.children:
            scan_meshes(child, meshes)

    meshes = []
    scan_meshes(bpy_obj, meshes)

    bbox = None
    spheres = []
    for mesh in meshes:
        if cache.get(mesh.name, None):
            bbx, center, radius = cache[mesh.name]
        else:
            if apply_transforms:
                mat_world = mesh.matrix_world
            else:
                mat_world = mathutils.Matrix()
            bmesh = convert_object_to_space_bmesh(mesh, mat_world)
            bbx = calculate_mesh_bbox(bmesh.verts, mat=mat_world)
            center, radius = calculate_mesh_bsphere(bbx, bmesh.verts, mat=mat_world)
            cache[mesh.name] = bbx, center, radius

        if bbox is None:
            bbox = bbx
        else:
            for i in range(3):
                bbox[0][i] = min(bbox[0][i], bbx[0][i])
                bbox[1][i] = max(bbox[1][i], bbx[1][i])
        spheres.append((center, radius))

    center = mathutils.Vector()
    radius = 0
    if not spheres:
        return (mathutils.Vector(), mathutils.Vector()), (center, radius)
    for sphere in spheres:
        center += sphere[0]
    center /= len(spheres)
    for ctr, rad in spheres:
        radius = max(radius, (ctr - center).length + rad)
    return bbox, (center, radius)


def top_two(dic):
    def top_one(dic, skip=None):
        max_key = None
        max_val = -1
        for key, val in dic.items():
            if (key != skip) and (val > max_val):
                max_val = val
                max_key = key
        return max_key, max_val

    key0, val0 = top_one(dic)
    key1, val1 = top_one(dic, key0)
    return {key0: val0, key1: val1}


def pw_v3f(vec):
    return vec[0], vec[2], vec[1]


def _export_child(bpy_obj, cwriter, context, vgm):
    bmesh = convert_object_to_space_bmesh(bpy_obj, mathutils.Matrix.Identity(4))
    bbox = calculate_mesh_bbox(bmesh.verts)
    bsph = calculate_mesh_bsphere(bbox, bmesh.verts)
    triangulate(bmesh, faces=bmesh.faces)
    bpy_data = bpy.data.meshes.new('.export-ogf')
    bmesh.to_mesh(bpy_data)

    cwriter.put(
        Chunks.HEADER,
        PackedWriter()
        .putf('B', 4)  # ogf version
        .putf('B', ModelType.SKELETON_GEOMDEF_ST)
        .putf('H', 0)  # shader id
        .putf('fff', *pw_v3f(bbox[0])).putf('fff', *pw_v3f(bbox[1]))
        .putf('fff', *pw_v3f(bsph[0])).putf('f', bsph[1])
    )

    material = bpy_obj.data.materials[0]
    texture = None
    if IS_28:
        if material.use_nodes:
            for node in material.node_tree.nodes:
                if node.type == 'TEX_IMAGE':
                    texture = node
        else:
            raise AppError('Material "{}" cannot use nodes.'.format(material.name))
    else:
        texture = material.active_texture
    cwriter.put(
        Chunks.TEXTURE,
        PackedWriter()
        .puts(
            gen_texture_name(texture, context.textures_folder)
            if context.texname_from_path else
            texture.name
        )
        .puts(material.xray.eshader)
    )

    bml_uv = bmesh.loops.layers.uv.active
    bml_vw = bmesh.verts.layers.deform.verify()
    bpy_data.calc_tangents(uvmap=bml_uv.name)
    vertices = []
    indices = []
    vmap = {}
    for face in bmesh.faces:
        face_indices = []
        for loop_index, loop in enumerate(face.loops):
            data_loop = bpy_data.loops[face.index * 3 + loop_index]
            uv = loop[bml_uv].uv
            vtx = (
                loop.vert.index,
                loop.vert.co.to_tuple(),
                data_loop.normal.to_tuple(),
                data_loop.tangent.to_tuple(),
                data_loop.bitangent.normalized().to_tuple(),
                (uv[0], 1 - uv[1]),
            )
            vertex_index = vmap.get(vtx)
            if vertex_index is None:
                vmap[vtx] = vertex_index = len(vertices)
                vertices.append(vtx)
            face_indices.append(vertex_index)
        indices.append(face_indices)

    vwmx = 0
    for vertex in bmesh.verts:
        vwc = len(vertex[bml_vw])
        if vwc > vwmx:
            vwmx = vwc

    fix_ensure_lookup_table(bmesh.verts)
    pwriter = PackedWriter()
    if vwmx == 1:
        pwriter.putf('II', VertexFormat.FVF_1L, len(vertices))
        for vertex in vertices:
            weights = bmesh.verts[vertex[0]][bml_vw]
            pwriter.putf('fff', *pw_v3f(vertex[1]))
            pwriter.putf('fff', *pw_v3f(vertex[2]))
            pwriter.putf('fff', *pw_v3f(vertex[3]))
            pwriter.putf('fff', *pw_v3f(vertex[4]))
            pwriter.putf('ff', *vertex[5])
            pwriter.putf('I', vgm[weights.keys()[0]])
    else:
        if vwmx != 2:
            print('warning: vwmx=%i' % vwmx)
        pwriter.putf('II', VertexFormat.FVF_2L, len(vertices))
        for vertex in vertices:
            weights = bmesh.verts[vertex[0]][bml_vw]
            if len(weights) > 2:
                weights = top_two(weights)
            weight = 0
            if len(weights) == 2:
                first = True
                weight0 = 0
                for vgi in weights.keys():
                    pwriter.putf('H', vgm[vgi])
                    if first:
                        weight0 = weights[vgi]
                        first = False
                    else:
                        weight = 1 - (weight0 / (weight0 + weights[vgi]))
            elif len(weights) == 1:
                for vgi in [vgm[_] for _ in weights.keys()]:
                    pwriter.putf('HH', vgi, vgi)
            else:
                raise Exception('oops: %i %s' % (len(weights), weights.keys()))
            pwriter.putf('fff', *pw_v3f(vertex[1]))
            pwriter.putf('fff', *pw_v3f(vertex[2]))
            pwriter.putf('fff', *pw_v3f(vertex[3]))
            pwriter.putf('fff', *pw_v3f(vertex[4]))
            pwriter.putf('f', weight)
            pwriter.putf('ff', *vertex[5])
    cwriter.put(Chunks.VERTICES, pwriter)

    pwriter = PackedWriter()
    pwriter.putf('I', 3 * len(indices))
    for face in indices:
        pwriter.putf('HHH', face[0], face[2], face[1])
    cwriter.put(Chunks.INDICES, pwriter)


def _export(bpy_obj, cwriter, context):
    bbox, bsph = calculate_bbox_and_bsphere(bpy_obj)
    cwriter.put(
        Chunks.HEADER,
        PackedWriter()
        .putf('B', 4)  # ogf version
        .putf('B', ModelType.SKELETON_ANIM if bpy_obj.xray.motionrefs else ModelType.SKELETON_RIGID)
        .putf('H', 0)  # shader id
        .putf('fff', *pw_v3f(bbox[0])).putf('fff', *pw_v3f(bbox[1]))
        .putf('fff', *pw_v3f(bsph[0])).putf('f', bsph[1])
    )

    cwriter.put(
        Chunks.S_DESC,
        PackedWriter()
        .puts(bpy_obj.name)
        .puts('blender')
        .putf('III', 0, 0, 0)
    )

    meshes = []
    bones = []
    bones_map = {}

    def reg_bone(bone, adv):
        idx = bones_map.get(bone, -1)
        if idx == -1:
            idx = len(bones)
            bones.append((bone, adv))
            bones_map[bone] = idx
        return idx

    def scan_r(bpy_obj):
        if is_helper_object(bpy_obj):
            return
        if bpy_obj.type == 'MESH':
            vgm = {}
            for modifier in bpy_obj.modifiers:
                if (modifier.type == 'ARMATURE') and modifier.object:
                    for i, group in enumerate(bpy_obj.vertex_groups):
                        bone = modifier.object.data.bones.get(group.name, None)
                        if bone is None:
                            raise AppError(
                                'bone "%s" not found in armature "%s" (for object "%s")' % (
                                    group.name, modifier.object.name, bpy_obj.name,
                                ),
                            )
                        vgm[i] = reg_bone(bone, modifier.object)
                    break  # use only first armature modifier
            mwriter = ChunkedWriter()
            _export_child(bpy_obj, mwriter, context, vgm)
            meshes.append(mwriter)
        elif bpy_obj.type == 'ARMATURE':
            for bone in bpy_obj.data.bones:
                if not is_exportable_bone(bone):
                    continue
                reg_bone(bone, bpy_obj)
        for child in bpy_obj.children:
            scan_r(child)

    scan_r(bpy_obj)

    ccw = ChunkedWriter()
    idx = 0
    for mwriter in meshes:
        ccw.put(idx, mwriter)
        idx += 1
    cwriter.put(Chunks.CHILDREN, ccw)

    pwriter = PackedWriter()
    pwriter.putf('I', len(bones))
    for bone, _ in bones:
        b_parent = find_bone_exportable_parent(bone)
        pwriter.puts(bone.name)
        pwriter.puts(b_parent.name if b_parent else '')
        xray = bone.xray
        pwriter.putf('fffffffff', *xray.shape.box_rot)
        pwriter.putf('fff', *xray.shape.box_trn)
        pwriter.putf('fff', *xray.shape.box_hsz)
    cwriter.put(Chunks.S_BONE_NAMES, pwriter)

    pwriter = PackedWriter()
    for bone, obj in bones:
        pbone = obj.pose.bones[bone.name]
        xray = bone.xray
        pwriter.putf('I', 0x1)  # version
        pwriter.puts(xray.gamemtl)
        pwriter.putf('H', int(xray.shape.type))
        pwriter.putf('H', xray.shape.flags)
        pwriter.putf('fffffffff', *xray.shape.box_rot)
        pwriter.putf('fff', *xray.shape.box_trn)
        pwriter.putf('fff', *xray.shape.box_hsz)
        pwriter.putf('fff', *xray.shape.sph_pos)
        pwriter.putf('f', xray.shape.sph_rad)
        pwriter.putf('fff', *xray.shape.cyl_pos)
        pwriter.putf('fff', *xray.shape.cyl_dir)
        pwriter.putf('f', xray.shape.cyl_hgh)
        pwriter.putf('f', xray.shape.cyl_rad)
        pwriter.putf('I', int(xray.ikjoint.type))
        pwriter.putf('ff', xray.ikjoint.lim_x_min, xray.ikjoint.lim_x_max)
        pwriter.putf('ff', xray.ikjoint.lim_x_spr, xray.ikjoint.lim_x_dmp)
        pwriter.putf('ff', xray.ikjoint.lim_y_min, xray.ikjoint.lim_y_max)
        pwriter.putf('ff', xray.ikjoint.lim_y_spr, xray.ikjoint.lim_y_dmp)
        pwriter.putf('ff', xray.ikjoint.lim_z_min, xray.ikjoint.lim_z_max)
        pwriter.putf('ff', xray.ikjoint.lim_z_spr, xray.ikjoint.lim_z_dmp)
        pwriter.putf('ff', xray.ikjoint.spring, xray.ikjoint.damping)
        pwriter.putf('I', xray.ikflags)
        pwriter.putf('ff', xray.breakf.force, xray.breakf.torque)
        pwriter.putf('f', xray.friction)
        mwriter = obj.matrix_world
        mat = multiply(mwriter, bone.matrix_local, MATRIX_BONE_INVERTED)
        b_parent = find_bone_exportable_parent(bone)
        if b_parent:
            mat = multiply(multiply(
                mwriter, b_parent.matrix_local, MATRIX_BONE_INVERTED
            ).inverted(), mat)
        euler = mat.to_euler('YXZ')
        pwriter.putf('fff', -euler.x, -euler.z, -euler.y)
        pwriter.putf('fff', *pw_v3f(mat.to_translation()))
        pwriter.putf('ffff', xray.mass.value, *pw_v3f(xray.mass.center))
    cwriter.put(Chunks.S_IKDATA, pwriter)

    cwriter.put(Chunks.S_USERDATA, PackedWriter().puts(bpy_obj.xray.userdata))
    if bpy_obj.xray.motionrefs:
        cwriter.put(Chunks.S_MOTION_REFS_0, PackedWriter().puts(bpy_obj.xray.motionrefs))


def export_file(bpy_obj, fpath, context):
    cwriter = ChunkedWriter()
    _export(bpy_obj, cwriter, context)
    save_file(fpath, cwriter)
