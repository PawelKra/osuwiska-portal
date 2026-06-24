import os
import dash
from dash import html, dcc
import dash_leaflet as dl

DB_URL = (
    "postgresql://{user}:{password}@{host}:{port}/{db}".format(
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        db=os.environ["DB_NAME"],
    )
)

app = dash.Dash(__name__)
app.title = "Portal Osuwisk"

app.layout = html.Div(
    [
        html.H1("Portal Osuwisk", style={"textAlign": "center", "marginBottom": "10px"}),
        dl.Map(
            center=[50.0, 20.0],
            zoom=7,
            children=[
                dl.TileLayer(
                    url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                    attribution="© OpenStreetMap contributors",
                ),
            ],
            style={"height": "calc(100vh - 80px)", "width": "100%"},
            id="main-map",
        ),
    ],
    style={"fontFamily": "sans-serif"},
)


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8999,
        debug=True,
        dev_tools_hot_reload=True,
        dev_tools_hot_reload_interval=1000,
        dev_tools_hot_reload_watch_interval=500,
    )
