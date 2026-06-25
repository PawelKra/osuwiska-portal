window.dashExtensions = window.dashExtensions || {};
window.dashExtensions.default = window.dashExtensions.default || {};
window.dashExtensions.debugClicks = window.dashExtensions.debugClicks || false;

// style prop – resolved via window path by dash-leaflet
window.dashExtensions.default.subareaStyle = function(feature) {
    if (feature.properties && feature.properties.style) {
        return feature.properties.style;
    }
    return {fillColor: "#AAAAAA", color: "#666", weight: 0.8, fillOpacity: 0.7};
};

// onEachFeature fallback – setStyle called per feature, always works
window.dashExtensions.default.onEachSubarea = function(feature, layer) {
    if (feature.properties && feature.properties.style) {
        layer.setStyle(feature.properties.style);
    }
    layer.on("click", function(e) {
        if (window.dashExtensions.debugClicks) {
            var latlng = e && e.latlng ? [e.latlng.lat, e.latlng.lng] : null;
            console.log("[subarea click]", {
                latlng: latlng,
                properties: feature.properties || null
            });
        }
    });
};

window.dashExtensions.default.logMapClick = function(e) {
    if (window.dashExtensions.debugClicks) {
        var latlng = e && e.latlng ? [e.latlng.lat, e.latlng.lng] : null;
        console.log("[map click]", {
            latlng: latlng,
            type: e ? e.type : null
        });
    }
};
