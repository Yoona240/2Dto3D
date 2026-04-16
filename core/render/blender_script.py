"""
Shared Blender Python script generation for rendering.
Used by both pipeline stage and standalone scripts.
"""

def generate_2d_render_script(
    glb_path_arg_index: int = 0,
    output_dir_arg_index: int = 1,
    views: list = None, 
    image_size_arg: str = "image_size",
    samples_arg: str = "samples",
    rotation_arg: str = "rotation_z",
    lighting_mode_arg: str = "lighting_mode"
) -> str:
    """
    Generate the Blender Python script content.
    
    The generated script expects arguments to be passed after "--".
    
    Args:
        glb_path_arg_index: Index of GLB path in argv after "--"
        output_dir_arg_index: Index of output dir in argv after "--"
        views: List of view tuples (name, location, rotation). If None, uses default 6 views.
        image_size_arg: variable name or value for image size
        samples_arg: variable name or value for samples
        rotation_arg: variable name or value for rotation_z
        lighting_mode_arg: variable name or value for lighting_mode
        
    Returns:
        String containing the full Blender Python script.
    """
    
    # Default 6 views if not provided
    if views is None:
        camera_distance = 2.0
        views_data = [
            ("front",  (0, -camera_distance, 0), (90, 0, 0)),
            ("back",   (0, camera_distance, 0), (90, 0, 180)),
            ("left",   (camera_distance, 0, 0), (90, 0, 90)),
            ("right",  (-camera_distance, 0, 0), (90, 0, -90)),
            ("top",    (0, 0, camera_distance), (0, 0, 0)),
            ("bottom", (0, 0, -camera_distance), (180, 0, 0)),
        ]
    else:
        views_data = views

    views_code = "views = [\n"
    for v in views_data:
        # v is (name, location, rotation)
        views_code += f'    {repr(v)},\n'
    views_code += "]"

    return f'''
import bpy
import mathutils
import math
import sys
import os

# Get arguments after "--"
argv = sys.argv
try:
    argv = argv[argv.index("--") + 1:]
except ValueError:
    print("Error: No arguments passed after '--'")
    sys.exit(1)

# Parse arguments based on provided indices or variable names in template
glb_path = argv[{glb_path_arg_index}]
output_dir = argv[{output_dir_arg_index}]

# Optional args handling (logic inside the script to safely get them)
def get_arg(index, default):
    return argv[index] if len(argv) > index else default

# These variables are expected to be injected or retrieved from argv
image_size = int({image_size_arg})
samples = int({samples_arg})
rotation_z = float({rotation_arg})
lighting_mode = str({lighting_mode_arg})

print(f"Loading: {{glb_path}}")
print(f"Output: {{output_dir}}")
print(f"Lighting: {{lighting_mode}}")

# Clear scene
bpy.ops.wm.read_factory_settings(use_empty=True)

# Import GLB
bpy.ops.import_scene.gltf(filepath=glb_path)

# Get all mesh objects
mesh_objects = [obj for obj in bpy.context.scene.objects if obj.type == 'MESH']

if not mesh_objects:
    print("No mesh objects found!")
    sys.exit(1)

# Apply Rotation Correction
if rotation_z != 0:
    rot_mat = mathutils.Matrix.Rotation(math.radians(rotation_z), 4, 'Z')
    for obj in mesh_objects:
        obj.matrix_world = rot_mat @ obj.matrix_world
    bpy.context.view_layer.update()

# Calculate bounding box
min_coord = mathutils.Vector((float('inf'), float('inf'), float('inf')))
max_coord = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))

for obj in mesh_objects:
    for corner in obj.bound_box:
        world_corner = obj.matrix_world @ mathutils.Vector(corner)
        min_coord.x = min(min_coord.x, world_corner.x)
        min_coord.y = min(min_coord.y, world_corner.y)
        min_coord.z = min(min_coord.z, world_corner.z)
        max_coord.x = max(max_coord.x, world_corner.x)
        max_coord.y = max(max_coord.y, world_corner.y)
        max_coord.z = max(max_coord.z, world_corner.z)

# Normalize
center = (min_coord + max_coord) / 2
size = max_coord - min_coord
max_dim = max(size.x, size.y, size.z)
scale_factor = 1.0 / max_dim if max_dim > 0 else 1.0

for obj in mesh_objects:
    obj.location -= center
    obj.scale *= scale_factor

bpy.context.view_layer.update()

# Setup render engine
bpy.context.scene.render.engine = 'CYCLES'
bpy.context.scene.cycles.samples = samples
bpy.context.scene.cycles.use_denoising = True

# Emit mode targets "texture/base-color only" output, so use Eevee for speed.
if lighting_mode == "emit":
    # Blender 4.x uses BLENDER_EEVEE_NEXT, while older versions use BLENDER_EEVEE.
    try:
        bpy.context.scene.render.engine = 'BLENDER_EEVEE_NEXT'
    except Exception:
        bpy.context.scene.render.engine = 'BLENDER_EEVEE'

# ============================================================
# Color Management - Fix "gray fog" issue
# ============================================================
# Filmic (default) compresses contrast, making images look washed out
# Standard gives more accurate colors for product/asset rendering
bpy.context.scene.view_settings.view_transform = 'Standard'
bpy.context.scene.view_settings.look = 'None'
bpy.context.scene.view_settings.exposure = 0.0
bpy.context.scene.view_settings.gamma = 1.0

# Use GPU if available
def setup_gpu_rendering():
    \"\"\"Try to enable GPU rendering with available compute device.\"\"\"
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        
        # Try different compute device types in order of preference
        # OptiX is fastest on RTX cards, then CUDA, then HIP (AMD), then Metal (Mac)
        device_types = ['OPTIX', 'CUDA', 'HIP', 'METAL', 'ONEAPI']
        
        gpu_enabled = False
        for device_type in device_types:
            try:
                prefs.compute_device_type = device_type
                # Refresh device list - this is required!
                prefs.get_devices()
                
                # Enable all available devices of this type
                cuda_devices = []
                for device in prefs.devices:
                    if device.type == device_type or device.type == 'CPU':
                        device.use = True
                        if device.type != 'CPU':
                            cuda_devices.append(device.name)
                
                if cuda_devices:
                    # Set scene to use GPU
                    bpy.context.scene.cycles.device = 'GPU'
                    print(f"GPU Rendering enabled: {{device_type}} - {{cuda_devices}}")
                    gpu_enabled = True
                    break
            except Exception as e:
                continue
        
        if not gpu_enabled:
            print("No GPU available, using CPU rendering")
            bpy.context.scene.cycles.device = 'CPU'
            
    except Exception as e:
        print(f"GPU setup failed: {{e}}, using CPU")
        bpy.context.scene.cycles.device = 'CPU'

setup_gpu_rendering()

# GPU memory optimization - use smaller tiles to reduce peak memory usage
# This is critical for avoiding OOM errors
try:
    # For Cycles, smaller tiles = less GPU memory (but slightly slower)
    bpy.context.scene.cycles.tile_size = 256
    
    # Enable persistent images to avoid reloading textures
    bpy.context.scene.render.use_persistent_data = True
    
    # Limit texture size to reduce memory (options: '128', '256', '512', '1024', '2048', '4096', '8192')
    bpy.context.scene.cycles.texture_limit = '2048'
    
    # Use OptiX denoising if available (faster and less memory than OpenImageDenoise)
    try:
        bpy.context.scene.cycles.denoiser = 'OPTIX'
    except:
        bpy.context.scene.cycles.denoiser = 'OPENIMAGEDENOISE'
except Exception as e:
    print(f"GPU memory optimization failed: {{e}}")

# Render settings
bpy.context.scene.render.resolution_x = image_size
bpy.context.scene.render.resolution_y = image_size
bpy.context.scene.render.image_settings.file_format = 'PNG'
bpy.context.scene.render.film_transparent = False

# ============================================================
# Lighting Setup
# ============================================================

# Helper to add sun light
def add_sun_no_shadow(location, energy):
    bpy.ops.object.light_add(type='SUN', location=location)
    light = bpy.context.object
    light.data.energy = energy
    light.data.use_shadow = False
    light.data.angle = 0
    return light

if lighting_mode == "emit":
    # EMIT - Preserve source texture/base color as much as possible:
    # replace surface shaders with Emission and remove scene lighting.
    world = bpy.data.worlds.new("EmitWhiteBackground")
    world.use_nodes = True
    nodes = world.node_tree.nodes
    links = world.node_tree.links
    nodes.clear()

    # Keep world non-emissive for non-camera rays, while camera sees pure white.
    # This gives white background without contaminating object colors.
    bg_black = nodes.new('ShaderNodeBackground')
    bg_black.inputs['Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    bg_black.inputs['Strength'].default_value = 0.0

    bg_white = nodes.new('ShaderNodeBackground')
    bg_white.inputs['Color'].default_value = (1.0, 1.0, 1.0, 1.0)
    bg_white.inputs['Strength'].default_value = 1.0

    light_path = nodes.new('ShaderNodeLightPath')
    mix_shader = nodes.new('ShaderNodeMixShader')

    output_node = nodes.new('ShaderNodeOutputWorld')
    links.new(bg_black.outputs['Background'], mix_shader.inputs[1])
    links.new(bg_white.outputs['Background'], mix_shader.inputs[2])
    links.new(light_path.outputs['Is Camera Ray'], mix_shader.inputs['Fac'])
    links.new(mix_shader.outputs['Shader'], output_node.inputs['Surface'])
    bpy.context.scene.world = world

    for obj in mesh_objects:
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue

            material.use_nodes = True
            node_tree = material.node_tree
            if node_tree is None:
                continue

            nodes = node_tree.nodes
            links = node_tree.links

            output = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
            if output is None:
                continue

            principled = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
            if principled is None:
                continue

            emission = nodes.new('ShaderNodeEmission')
            emission.inputs['Strength'].default_value = 1.0

            base_color_input = principled.inputs.get('Base Color')
            if base_color_input is not None and base_color_input.is_linked:
                source_socket = base_color_input.links[0].from_socket
                links.new(source_socket, emission.inputs['Color'])
            elif base_color_input is not None:
                emission.inputs['Color'].default_value = base_color_input.default_value
            else:
                emission.inputs['Color'].default_value = (1.0, 1.0, 1.0, 1.0)

            for link in list(output.inputs['Surface'].links):
                links.remove(link)
            links.new(emission.outputs['Emission'], output.inputs['Surface'])

elif lighting_mode == "ambient":
    # AMBIENT ONLY - Pure environmental light
    world = bpy.data.worlds.new("White")
    world.use_nodes = True
    nodes = world.node_tree.nodes
    links = world.node_tree.links
    nodes.clear()
    
    bg_node = nodes.new('ShaderNodeBackground')
    bg_node.inputs['Color'].default_value = (1.0, 1.0, 1.0, 1.0)
    bg_node.inputs['Strength'].default_value = 1.0  # Moderate ambient
    
    output_node = nodes.new('ShaderNodeOutputWorld')
    links.new(bg_node.outputs['Background'], output_node.inputs['Surface'])
    bpy.context.scene.world = world

elif lighting_mode == "flat":
    # HYBRID FLAT - Ambient (0.8) + Soft Directional Fill (0.5-0.8)
    # Recommended for most cases to avoid black cavities
    
    world = bpy.data.worlds.new("White")
    world.use_nodes = True
    nodes = world.node_tree.nodes
    links = world.node_tree.links
    nodes.clear()
    
    bg_node = nodes.new('ShaderNodeBackground')
    bg_node.inputs['Color'].default_value = (1.0, 1.0, 1.0, 1.0)
    bg_node.inputs['Strength'].default_value = 0.8
    
    output_node = nodes.new('ShaderNodeOutputWorld')
    links.new(bg_node.outputs['Background'], output_node.inputs['Surface'])
    bpy.context.scene.world = world
    
    # 6-Directional Fill - lights positioned to AVOID direct reflection into camera
    # Each light is offset from the camera axis to prevent specular hotspots
    add_sun_no_shadow((1, -3, 1), 0.6)    # Front (offset X)
    add_sun_no_shadow((-1, 3, 1), 0.6)   # Back (offset X)
    add_sun_no_shadow((3, 1, 1), 0.6)    # Left (offset Y)
    add_sun_no_shadow((-3, -1, 1), 0.6)  # Right (offset Y)
    # Top: use 4 corner lights instead of one center light to avoid reflection in top view
    add_sun_no_shadow((2, 2, 4), 0.3)    # Top-corner 1
    add_sun_no_shadow((-2, 2, 4), 0.3)   # Top-corner 2
    add_sun_no_shadow((2, -2, 4), 0.3)   # Top-corner 3
    add_sun_no_shadow((-2, -2, 4), 0.3)  # Top-corner 4
    # Bottom: offset to avoid reflection in bottom view
    add_sun_no_shadow((1, 1, -3), 0.4)   # Bottom (offset)

else:
    # STUDIO - Traditional 3-point lighting with shadows
    world = bpy.data.worlds.new("White Background")
    world.use_nodes = True
    bg_node = world.node_tree.nodes.get("Background")
    if bg_node:
        bg_node.inputs[0].default_value = (1, 1, 1, 1)
        bg_node.inputs[1].default_value = 1.0
    bpy.context.scene.world = world
    
    # Key light
    bpy.ops.object.light_add(type='SUN', location=(2, -2, 3))
    key_light = bpy.context.object
    key_light.data.energy = 3.0
    key_light.rotation_euler = (math.radians(45), math.radians(15), math.radians(45))
    
    # Fill light
    bpy.ops.object.light_add(type='SUN', location=(-2, -1, 2))
    fill_light = bpy.context.object
    fill_light.data.energy = 1.5
    
    # Rim light
    bpy.ops.object.light_add(type='SUN', location=(0, 2, 1))
    rim_light = bpy.context.object
    rim_light.data.energy = 2.0

# ============================================================
# Camera Setup
# ============================================================

bpy.ops.object.camera_add()
camera = bpy.context.object
bpy.context.scene.camera = camera
camera.data.type = 'ORTHO'
camera.data.ortho_scale = 1.2

# Views
{views_code}

os.makedirs(output_dir, exist_ok=True)

for view_name, location, rotation in views:
    camera.location = location
    camera.rotation_euler = tuple(math.radians(r) for r in rotation)
    
    output_path = os.path.join(output_dir, f"{{view_name}}.png")
    bpy.context.scene.render.filepath = output_path
    
    print(f"Rendering {{view_name}}...")
    bpy.ops.render.render(write_still=True)
    print(f"  Saved: {{output_path}}")

print("\\n[Success] All views rendered successfully!")
'''
