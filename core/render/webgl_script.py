"""
WebGL Render Script Generator

Generates HTML/JS for rendering 3D models using model-viewer in headless Chrome.
Used by the WebGL rendering backend as an alternative to Blender.

Camera orbit convention (model-viewer spherical coordinates):
  theta: azimuth angle in degrees (0=front, 90=left, -90=right, 180=back)
  phi:   polar angle from top in degrees (0=top, 90=equator, 180=bottom)
  radius: calculated dynamically from model bounding box diagonal
"""

from pathlib import Path


# (name, theta_deg, phi_deg)
# Camera distance is computed at runtime from the model's bounding box.
DEFAULT_VIEWS = [
    ("front", 0, 90),
    ("back", 180, 90),
    ("left", 90, 90),
    ("right", -90, 90),
    ("top", 0, 0.001),  # almost exact pole view
    ("bottom", 0, 179.999),  # almost exact opposite pole
]


def generate_render_html(
    glb_url: str,
    image_size: int = 512,
    environment_image: str = "neutral",
    shadow_intensity: float = 1.0,
) -> str:
    """
    Generate HTML content for rendering a GLB model with model-viewer.

    Camera distance is calculated dynamically at runtime from the model's
    bounding-box diagonal, so the object is always fully visible in every view.
    View iteration is driven externally by Playwright via window.setView().

    Args:
        glb_url: URL to the GLB file (http:// or file://)
        image_size: Size of the rendered images (width and height)
        environment_image: Environment image for IBL lighting
        shadow_intensity: Shadow intensity (0-2)

    Returns:
        HTML content as string
    """
    # Get the path to local model-viewer.js
    model_viewer_js_path = (
        Path(__file__).parent.parent.parent / "static" / "js" / "model-viewer.min.js"
    )
    with open(model_viewer_js_path, "r") as f:
        model_viewer_js_content = f.read()

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>WebGL Render</title>
    <style>
        * {{ margin: 0; padding: 0; }}
        body {{ background: #ffffff; overflow: hidden; }}
        model-viewer {{
            width: {image_size}px;
            height: {image_size}px;
            background-color: #ffffff;
            --poster-color: #ffffff;
        }}
    </style>
</head>
<body>

<model-viewer
    id="viewer"
    src="{glb_url}"
    shadow-intensity="{shadow_intensity}"
    environment-image="{environment_image}"
    exposure="1.0"
    camera-controls
    interaction-prompt="none"
    loading="eager"
    interpolation-decay="0"
    min-camera-orbit="auto 0deg auto"
    max-camera-orbit="Infinity 180deg Infinity"
></model-viewer>

<script>
{model_viewer_js_content}
</script>

<script>
// Set Draco decoder location to the same directory as the GLB (served by local HTTP).
(async () => {{
    await customElements.whenDefined('model-viewer');
    const Cls = customElements.get('model-viewer');
    const viewer = document.getElementById('viewer');
    const srcUrl = viewer.getAttribute('src');
    Cls.dracoDecoderLocation = srcUrl.substring(0, srcUrl.lastIndexOf('/') + 1);
}})();
</script>

<script>
// Expose helpers for Playwright to drive the render loop externally.
(function() {{
    const viewer = document.getElementById('viewer');

    // Compute a camera distance that guarantees the full model is
    // visible from ANY angle (based on bounding-box diagonal + FOV).
    function computeSafeRadius() {{
        const dims = viewer.getDimensions();
        const diagonal = Math.sqrt(dims.x*dims.x + dims.y*dims.y + dims.z*dims.z);
        const fovDeg  = parseFloat(viewer.getFieldOfView()) || 45;
        const fovRad  = fovDeg * Math.PI / 180;
        const radius  = (diagonal / 2) / Math.tan(fovRad / 2) * 1.2;
        console.log('[WebGL] bbox', dims.x.toFixed(3), dims.y.toFixed(3), dims.z.toFixed(3),
                    'diag=' + diagonal.toFixed(3), 'fov=' + fovDeg + '°', 'radius=' + radius.toFixed(3) + 'm');
        return radius;
    }}

    // Called by Playwright after model loads.
    window._mvReady    = false;
    window._safeRadius = null;
    window._center     = null;

    viewer.addEventListener('load', () => {{
        console.log('[WebGL] model loaded');
        // Delay to let bounding box stabilise
        setTimeout(() => {{
            window._safeRadius = computeSafeRadius();
            window._center = viewer.getBoundingBoxCenter();
            viewer.cameraTarget = `${{window._center.x}}m ${{window._center.y}}m ${{window._center.z}}m`;
            window._mvReady = true;
            console.log(
                '[WebGL] ready, safeRadius=' + window._safeRadius.toFixed(3) +
                ', center=' + window._center.x.toFixed(3) + ',' +
                window._center.y.toFixed(3) + ',' + window._center.z.toFixed(3)
            );
        }}, 1500);
    }});

    viewer.addEventListener('error', (ev) => {{
        console.log('[WebGL] ERROR: ' + (ev.detail || ev));
    }});

    // Playwright calls this, then takes page.screenshot().
    window.setView = function(name, theta, phi) {{
        const r = window._safeRadius;
        const c = window._center;
        viewer.cameraTarget = `${{c.x}}m ${{c.y}}m ${{c.z}}m`;
        // Use orthographic projection for strict top/bottom alignment.
        if (name === 'top' || name === 'bottom') {{
            viewer.cameraProjection = 'orthographic';
        }} else {{
            viewer.cameraProjection = 'perspective';
        }}
        viewer.cameraOrbit = theta + 'deg ' + phi + 'deg ' + r + 'm';
        viewer.jumpCameraToGoal();
        console.log(
            '[WebGL] setView ' + name + ' theta=' + theta + ' phi=' + phi + ' r=' + r.toFixed(3) +
            ' target=' + c.x.toFixed(3) + ',' + c.y.toFixed(3) + ',' + c.z.toFixed(3)
        );
    }};
}})();
</script>

</body>
</html>"""

    return html_content
