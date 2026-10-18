# Open-Source Map Integration Guide (Leaflet.js & MapLibre GL)

This guide shows frontend developers how to render real-time delivery tracking and route history using 100% open-source map libraries, completely eliminating Google Maps recurring costs.

---

## 1. Quick Comparison: Leaflet vs. MapLibre GL

| Feature | Leaflet.js | MapLibre GL JS |
| :--- | :--- | :--- |
| **Bundle Size** | 42 KB (Extremely light) | ~200 KB (Vector engine) |
| **Rendering Tech** | HTML5 / Raster Tiles | WebGL / Vector Tiles (60 FPS) |
| **Car Heading / Rotation** | Via `leaflet-rotatedmarker` plugin | Built-in 360° bearing & 3D tilt |
| **Best Used For** | Fast mobile web views, low-bandwidth networks | Premium delivery apps (Uber / Deliveroo feel) |

---

## 2. Leaflet.js Implementation Example

### A. HTML & CSS Setup
```html
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<!-- Marker rotation plugin -->
<script src="https://unpkg.com/leaflet-rotatedmarker@0.2.0/leaflet.rotatedMarker.js"></script>

<div id="map" style="width: 100%; height: 500px; border-radius: 12px;"></div>
```

### B. JavaScript: Initializing Map & WebSocket Tracking
```javascript
// 1. Initialize Map centered on delivery area (e.g. Lagos, Nigeria)
const map = L.map('map').setView([6.5244, 3.3792], 13);

// 2. Add Free OpenStreetMap or CartoDB Positron Tiles
L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; OpenStreetMap contributors & CartoDB',
    maxZoom: 19
}).addTo(map);

// 3. Define Driver Icon
const driverIcon = L.icon({
    iconUrl: '/assets/delivery-bike.png',
    iconSize: [36, 36],
    iconAnchor: [18, 18],
});

let driverMarker = null;
let routePolyline = L.polyline([], { color: '#2D6A4F', weight: 4, opacity: 0.8 }).addTo(map);

// 4. Fetch Historical Breadcrumbs on Page Load
async function loadRouteHistory(orderId, token) {
    const res = await fetch(`/orders/${orderId}/tracking/history`, {
        headers: { 'Authorization': `Bearer ${token}` }
    });
    if (res.ok) {
        const data = await res.json();
        const latLngs = data.breadcrumbs.map(b => [b.latitude, b.longitude]);
        routePolyline.setLatLngs(latLngs);
        if (latLngs.length > 0) {
            const last = latLngs[latLngs.length - 1];
            updateDriverMarker(last[0], last[1], data.breadcrumbs[data.breadcrumbs.length - 1].heading);
            map.panTo(last);
        }
    }
}

// 5. Connect to Real-Time WebSocket Ping Stream
function connectTrackingSocket(orderId, token) {
    const ws = new WebSocket(`wss://api.farmconnect.ng/ws/orders/${orderId}?token=${token}`);

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.event === 'delivery_location_ping') {
            updateDriverMarker(data.latitude, data.longitude, data.heading);
            routePolyline.addLatLng([data.latitude, data.longitude]);
            
            // Update UI widgets
            document.getElementById('eta-text').innerText = data.eta_minutes ? `${data.eta_minutes} mins away` : 'Calculating...';
            document.getElementById('distance-text').innerText = data.distance_text || '';
        }
    };
}

function updateDriverMarker(lat, lng, heading = 0) {
    if (!driverMarker) {
        driverMarker = L.marker([lat, lng], {
            icon: driverIcon,
            rotationAngle: heading || 0,
            rotationOrigin: 'center center'
        }).addTo(map);
    } else {
        driverMarker.setLatLng([lat, lng]);
        if (heading !== null && heading !== undefined) {
            driverMarker.setRotationAngle(heading);
        }
    }
}
```

---

## 3. MapLibre GL JS Implementation Example (Smooth 60 FPS)

```html
<link href="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.css" rel="stylesheet" />
<script src="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.js"></script>

<div id="map" style="width: 100%; height: 500px;"></div>

<script>
const map = new maplibregl.Map({
    container: 'map',
    style: 'https://demotiles.maplibre.org/style.json', // Or Stadia/Carto vector style
    center: [3.3792, 6.5244], // [lng, lat]
    zoom: 14,
    pitch: 45 // 3D perspective
});

const marker = new maplibregl.Marker({ rotationAlignment: 'map' })
    .setLngLat([3.3792, 6.5244])
    .addTo(map);

// In WebSocket message handler:
function onLocationPing(data) {
    marker.setLngLat([data.longitude, data.latitude]);
    if (data.heading !== null && data.heading !== undefined) {
        marker.setRotation(data.heading);
    }
    map.easeTo({ center: [data.longitude, data.latitude], duration: 1000 });
}
</script>
```

---

## 4. Backend Endpoints Summary

- `PATCH /orders/{order_id}/location` (Driver): Posts GPS coordinates (`latitude`, `longitude`, `heading`, `speed`, `accuracy`).
- `GET /orders/{order_id}/tracking` (All): Snapshot with destination, driver coords, ETA, and distance.
- `GET /orders/{order_id}/tracking/history` (All): Ordered breadcrumbs array for drawing the route line.
- `WS /ws/orders/{order_id}`: Broadcasts real-time `delivery_location_ping` to all connected parties.
