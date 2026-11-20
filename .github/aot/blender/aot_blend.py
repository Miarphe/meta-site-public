import json
import math
import os
import re
import sys

import bpy


def parse(step):
    m = re.match(r"^\s*(\w+)\s*(?:\((.*)\))?\s*$", step)
    if not m:
        raise SystemExit("bad step: " + step)
    args = []
    if m.group(2):
        for a in m.group(2).split(","):
            a = a.strip().strip("\"'")
            try:
                args.append(float(a) if "." in a else int(a))
            except ValueError:
                args.append(a)
    return m.group(1), args


def clear():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def load(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    else:
        raise SystemExit("unsupported input: " + path)


def meshes():
    return [o for o in bpy.context.scene.objects if o.type == "MESH"]


def armature():
    for o in bpy.context.scene.objects:
        if o.type == "ARMATURE":
            return o
    return None


def select_all():
    bpy.ops.object.select_all(action="DESELECT")
    for o in bpy.context.scene.objects:
        if o.type in ("MESH", "ARMATURE"):
            o.select_set(True)
    objs = [o for o in bpy.context.scene.objects if o.select_get()]
    if objs:
        bpy.context.view_layer.objects.active = objs[0]


def s_decimate(job, ratio=0.5):
    for o in meshes():
        bpy.context.view_layer.objects.active = o
        mod = o.modifiers.new("aot_decimate", "DECIMATE")
        mod.ratio = float(ratio)
        if o.data.shape_keys is None:
            bpy.ops.object.modifier_apply(modifier=mod.name)
        else:
            o.modifiers.remove(mod)


def s_retarget(job, path):
    target = armature()
    if target is None:
        raise SystemExit("retarget needs an armature in the input")
    before = set(bpy.context.scene.objects)
    actions_before = set(bpy.data.actions)
    load(os.path.join(job["root"], str(path)))
    added = [o for o in bpy.context.scene.objects if o not in before]
    new_actions = [a for a in bpy.data.actions if a not in actions_before]
    for o in added:
        bpy.data.objects.remove(o, do_unlink=True)
    if target.animation_data is None:
        target.animation_data_create()
    for a in new_actions:
        a.use_fake_user = True
        tr = target.animation_data.nla_tracks.new()
        tr.name = a.name
        tr.strips.new(a.name, int(a.frame_range[0]), a)


def s_orient(job):
    select_all()
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    low = None
    for o in meshes():
        for c in o.bound_box:
            z = (o.matrix_world @ __import__("mathutils").Vector(c)).z
            low = z if low is None or z < low else low
    if low is not None:
        for o in bpy.context.scene.objects:
            if o.parent is None:
                o.location.z -= low


def s_export_glb(job):
    os.makedirs(os.path.dirname(job["glb"]), exist_ok=True)
    bpy.ops.export_scene.gltf(filepath=job["glb"], export_format="GLB", export_yup=True, export_animations=True, export_apply=False)


def frame_setup(size):
    sc = bpy.context.scene
    sc.render.engine = "BLENDER_WORKBENCH"
    sc.render.resolution_x = int(size)
    sc.render.resolution_y = int(size)
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    return sc


def bounds():
    import mathutils
    lo = mathutils.Vector((1e9, 1e9, 1e9))
    hi = mathutils.Vector((-1e9, -1e9, -1e9))
    for o in meshes():
        for c in o.bound_box:
            w = o.matrix_world @ mathutils.Vector(c)
            lo = mathutils.Vector((min(lo.x, w.x), min(lo.y, w.y), min(lo.z, w.z)))
            hi = mathutils.Vector((max(hi.x, w.x), max(hi.y, w.y), max(hi.z, w.z)))
    return lo, hi


def make_camera(angle_deg, dist, center, height):
    import mathutils
    cam = bpy.data.objects.get("aot_cam")
    if cam is None:
        cam = bpy.data.objects.new("aot_cam", bpy.data.cameras.new("aot_cam"))
        bpy.context.scene.collection.objects.link(cam)
        bpy.context.scene.camera = cam
    a = math.radians(angle_deg)
    cam.location = (center.x + math.sin(a) * dist, center.y - math.cos(a) * dist, center.z + height)
    direction = center - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return cam


def s_turntable(job, frames=12, size=512):
    frames = int(frames)
    sc = frame_setup(size)
    lo, hi = bounds()
    center = (lo + hi) / 2
    dist = max((hi - lo).length, 0.5) * 1.6
    for i in range(frames):
        make_camera(360.0 * i / frames, dist, center, (hi.z - lo.z) * 0.25)
        sc.render.filepath = os.path.join(job["previews"], "preview-turn-%02d.png" % (i + 1))
        bpy.ops.render.render(write_still=True)


def s_sprites(job, anim, frames=8, size=128):
    frames = int(frames)
    sc = frame_setup(size)
    arm = armature()
    action = bpy.data.actions.get(str(anim))
    if arm is not None and action is not None:
        if arm.animation_data is None:
            arm.animation_data_create()
        arm.animation_data.action = action
        lo_f, hi_f = int(action.frame_range[0]), int(action.frame_range[1])
    else:
        lo_f, hi_f = 1, 1
    lo, hi = bounds()
    center = (lo + hi) / 2
    dist = max((hi - lo).length, 0.5) * 1.8
    make_camera(0, dist, center, (hi.z - lo.z) * 0.1)
    out = os.path.join(job["sprites"], os.path.splitext(os.path.basename(job["out"]))[0] + "_" + str(anim))
    os.makedirs(out, exist_ok=True)
    for i in range(frames):
        f = lo_f + int((hi_f - lo_f) * i / max(1, frames))
        sc.frame_set(f)
        sc.render.filepath = os.path.join(out, "%02d.png" % i)
        bpy.ops.render.render(write_still=True)


STEPS = {
    "decimate": s_decimate,
    "retarget": s_retarget,
    "orient": s_orient,
    "export_glb": s_export_glb,
    "turntable": s_turntable,
    "sprites": s_sprites,
}


def main():
    argv = sys.argv
    job = json.load(open(argv[argv.index("--") + 1]))
    clear()
    load(job["input"])
    for step in job["steps"]:
        name, args = parse(step)
        if name not in STEPS:
            raise SystemExit("unknown step: " + name)
        print("STEP", name, args, flush=True)
        STEPS[name](job, *args)
    print("JOB DONE", job["out"], flush=True)


main()
