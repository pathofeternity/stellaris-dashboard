import logging
import random
import time
from typing import Dict, Any, List
from urllib import parse

import dash
import dash_core_components as dcc
import dash_html_components as html
import flask
import plotly.graph_objs as go
from dash.dependencies import Input, Output
from flask import render_template

from stellarisdashboard import config, models, visualization_data

logger = logging.getLogger(__name__)

flask_app = flask.Flask(__name__)
flask_app.logger.setLevel(logging.DEBUG)
timeline_app = dash.Dash(name="Stellaris Timeline", server=flask_app, compress=False, url_base_pathname="/timeline")
timeline_app.css.config.serve_locally = True
timeline_app.scripts.config.serve_locally = True

COLOR_PHYSICS = 'rgba(30,100,170,0.5)'
COLOR_SOCIETY = 'rgba(60,150,90,0.5)'
COLOR_ENGINEERING = 'rgba(190,150,30,0.5)'


@flask_app.route("/")
def index_page():
    games = [dict(country=country, game_name=g) for g, country in models.get_available_games_dict().items()]
    return render_template("index.html", games=games)


@flask_app.route("/history/<game_name>")
def history_page(game_name):
    games_dict = models.get_available_games_dict()
    if game_name not in games_dict:
        matches = list(models.get_game_names_matching(game_name))
        if not matches:
            logger.warning(f"Could not find a game matching {game_name}")
            return render_template("404_page.html", game_not_found=True, game_name=game_name)
        game_name = matches[0]
    country = games_dict[game_name]
    with models.get_db_session(game_name) as session:
        date = get_most_recent_date(session)
        wars = get_war_dicts(session, date)
        leaders = get_leader_dicts(session, date)
    return render_template("history_page.html", country=country, wars=wars, leaders=leaders)


DEFAULT_PLOT_LAYOUT = go.Layout(
    yaxis=dict(
        type="linear",
    ),
    height=640,
)

CATEGORY_TABS = [{'label': category, 'value': category} for category in visualization_data.THEMATICALLY_GROUPED_PLOTS]
CATEGORY_TABS.append({'label': "Galaxy", 'value': "Galaxy"})
DEFAULT_SELECTED_CATEGORY = "Economy"

timeline_app.layout = html.Div([
    dcc.Location(id='url', refresh=False),
    html.A("Return to index", id='index-link', href="/"),
    html.Div([
        dcc.Tabs(
            tabs=CATEGORY_TABS,
            value=DEFAULT_SELECTED_CATEGORY,
            id='tabs',
        ),
        html.Div(id='tab-content', style={
            'width': '100%',
            'margin-left': 'auto',
            'margin-right': 'auto'
        }),
        dcc.Slider(
            id='dateslider',
            min=0,
            max=100,
            step=0.001,
            value=100,
            updatemode='drag',
            marks={i: '{}%'.format(i) for i in range(0, 110, 10)},
        ),
    ], style={
        'width': '100%',
        'fontFamily': 'Sans-Serif',
        'margin-left': 'auto',
        'margin-right': 'auto'
    }),
])


def get_figure_layout(plot_spec: visualization_data.PlotSpecification):
    layout = DEFAULT_PLOT_LAYOUT
    if plot_spec.style == visualization_data.PlotStyle.stacked:
        layout["yaxis"] = {}
    return go.Layout(**layout)


@timeline_app.callback(Output('tab-content', 'children'),
                       [Input('tabs', 'value'), Input('url', 'search'), Input('dateslider', 'value')])
def update_content(tab_value, search, date_fraction):
    game_id = parse.parse_qs(parse.urlparse(search).query).get("game_name", [None])[0]
    if game_id is None:
        game_id = ""
    available_games = models.get_available_games_dict()
    if game_id not in available_games:
        for g in available_games:
            if g.startswith(game_id):
                logger.info(f"Found game {g} matching prefix {game_id}!")
                game_id = g
                break
        else:
            logger.warning(f"Game {game_id} does not match any known game!")
            return []
    logger.info(f"dash_server.update_content: Tab is {tab_value}, Game is {game_id}")
    with models.get_db_session(game_id) as session:
        current_date = get_most_recent_date(session)

    children = [html.H1(f"{available_games[game_id]} ({game_id})")]
    if tab_value in visualization_data.THEMATICALLY_GROUPED_PLOTS:
        plots = visualization_data.THEMATICALLY_GROUPED_PLOTS[tab_value]
        for plot_spec in plots:
            figure_data = get_figure_data(game_id, plot_spec)
            figure_layout = get_figure_layout(plot_spec)
            figure = go.Figure(data=figure_data, layout=figure_layout)

            children.append(html.H2(f"{plot_spec.title}"))
            children.append(dcc.Graph(
                id=f"{plot_spec.plot_id}",
                figure=figure,
            ))
    else:
        slider_date = 0.01 * date_fraction * current_date
        children.append(html.H2(f"Galactic Records for {models.days_to_date(slider_date)}"))
        children.append(get_galaxy(game_id, slider_date))
    return children


def get_galaxy(game_id, date):
    # adapted from https://plot.ly/python/network-graphs/
    galaxy = visualization_data.get_galaxy_data(game_id)
    graph = galaxy.get_graph_for_date(date)
    edge_traces = {}
    for edge in graph.edges:
        country = graph.edges[edge]["country"]
        if country not in edge_traces:
            width = 1 if country == visualization_data.GalaxyMapData.UNCLAIMED else 3
            edge_traces[country] = go.Scatter(
                x=[],
                y=[],
                line=go.Line(width=width, color=get_country_color(country)),
                hoverinfo='none',
                mode='lines',
                showlegend=False,
            )
        x0, y0 = graph.nodes[edge[0]]['pos']
        x1, y1 = graph.nodes[edge[1]]['pos']
        edge_traces[country]['x'] += [x0, x1, None]
        edge_traces[country]['y'] += [y0, y1, None]

    node_traces = {}
    for node in graph.nodes:
        country = graph.nodes[node]["country"]
        if country not in node_traces:
            node_traces[country] = go.Scatter(
                x=[], y=[],
                text=[],
                mode='markers',
                hoverinfo='text',
                marker=go.Marker(
                    color=[],
                    size=4,
                    line=dict(width=0.5)),
                name=country,
            )
        if country == visualization_data.GalaxyMapData.UNCLAIMED:
            color = "rgba(255,255,255,0.5)"
        else:
            color = get_country_color(country)
        node_traces[country]['marker']['color'].append(color)
        x, y = graph.nodes[node]['pos']
        node_traces[country]['x'].append(x)
        node_traces[country]['y'].append(y)
        country_str = f" ({country})" if country != visualization_data.GalaxyMapData.UNCLAIMED else ""
        node_traces[country]['text'].append(f'{graph.nodes[node]["name"]}{country_str}')

    layout = go.Layout(
        width=900,
        xaxis=go.XAxis(
            showgrid=False,
            zeroline=False,
            showticklabels=False
        ),
        yaxis=go.YAxis(
            showgrid=False,
            zeroline=False,
            showticklabels=False,
            scaleanchor='x',
            scaleratio=1,
        ),
        margin=dict(
            t=0, b=0, l=0, r=0,
        ),
        legend=dict(
            orientation="v",
            x=1.0,
            y=1.0,
        ),
        hovermode='closest',
    )

    return dcc.Graph(
        id="galaxy-map",
        figure=go.Figure(
            data=go.Data(list(edge_traces.values()) + list(node_traces.values())),
            layout=layout,
        ),
    )


def get_country_color(country_name: str) -> str:
    random.seed(country_name)
    r, g, b = [random.randint(20, 235) for _ in range(3)]
    color = f"rgba({r},{g},{b},1.0)"
    return color


def get_most_recent_date(session):
    most_recent_gs = session.query(models.GameState).order_by(models.GameState.date.desc()).first()
    if most_recent_gs is None:
        most_recent_date = 0
    else:
        most_recent_date = most_recent_gs.date
    return most_recent_date


def get_figure_data(game_id: str, plot_spec: visualization_data.PlotSpecification):
    start = time.time()
    plot_data = visualization_data.get_current_execution_plot_data(game_id)
    plot_list = get_plot_lines(plot_data, plot_spec)
    end = time.time()
    logger.info(f"Update took {end - start} seconds!")
    return plot_list


def get_plot_lines(plot_data: visualization_data.EmpireProgressionPlotData, plot_spec: visualization_data.PlotSpecification) -> List[Dict[str, Any]]:
    if plot_spec.style == visualization_data.PlotStyle.line:
        plot_list = _get_line_plot_data(plot_data, plot_spec)
    elif plot_spec.style == visualization_data.PlotStyle.stacked:
        plot_list = _get_stacked_plot_data(plot_data, plot_spec)
    elif plot_spec.style == visualization_data.PlotStyle.budget:
        plot_list = _get_budget_plot_data(plot_data, plot_spec)
    else:
        logger.warning(f"Unknown Plot type {plot_spec}")
        plot_list = []
    return sorted(plot_list, key=lambda p: p["y"][-1])


def _get_line_plot_data(plot_data: visualization_data.EmpireProgressionPlotData, plot_spec: visualization_data.PlotSpecification):
    plot_list = []
    for key, x_values, y_values in plot_data.data_sorted_by_last_value(plot_spec):
        if not any(y_values):
            continue
        line = {'x': x_values, 'y': y_values, 'name': key, "text": [f"{val:.2f} - {key}" for val in y_values]}
        plot_list.append(line)
    return plot_list


def _get_stacked_plot_data(plot_data: visualization_data.EmpireProgressionPlotData, plot_spec: visualization_data.PlotSpecification):
    y_previous = None
    plot_list = []
    for key, x_values, y_values in plot_data.iterate_data(plot_spec):
        if not any(y_values):
            continue
        line = {'x': x_values, 'name': key, "fill": "tonexty", "hoverinfo": "x+text"}
        if y_previous is None:
            y_previous = [0.0 for _ in x_values]
        y_previous = [(a + b) for a, b in zip(y_previous, y_values)]
        line["y"] = y_previous[:]  # make a copy
        if line["y"]:
            line["text"] = [f"{val:.2f} - {key}" if val else "" for val in y_values]
            if key == "physics":
                line["line"] = {"color": COLOR_PHYSICS}
                line["fillcolor"] = COLOR_PHYSICS
            elif key == "society":
                line["line"] = {"color": COLOR_SOCIETY}
                line["fillcolor"] = COLOR_SOCIETY
            elif key == "engineering":
                line["line"] = {"color": COLOR_ENGINEERING}
                line["fillcolor"] = COLOR_ENGINEERING
            plot_list.append(line)
    return plot_list


def _get_budget_plot_data(plot_data: visualization_data.EmpireProgressionPlotData, plot_spec: visualization_data.PlotSpecification):
    net_gain = None
    y_previous_pos, y_previous_neg = None, None
    pos_initiated = False
    plot_list = []
    for key, x_values, y_values in plot_data.data_sorted_by_last_value(plot_spec):
        if not any(y_values):
            continue
        if net_gain is None:
            net_gain = [0.0 for _ in x_values]
            y_previous_pos = [0.0 for _ in x_values]
            y_previous_neg = [0.0 for _ in x_values]
        fill_mode = "tozeroy"
        if all(y <= 0 for y in y_values):
            y_previous = y_previous_neg
        elif all(y >= 0 for y in y_values):
            y_previous = y_previous_pos
            if pos_initiated:
                fill_mode = "tonexty"
            pos_initiated = True
        else:
            logger.warning("Not a real budget Graph!")
            break
        line = {'x': x_values, 'name': key, "hoverinfo": "x+text"}
        for i, y in enumerate(y_values):
            y_previous[i] += y
            net_gain[i] += y
        line["y"] = y_previous[:]
        line["fill"] = fill_mode
        line["text"] = [f"{val:.2f} - {key}" if val else "" for val in y_values]
        plot_list.append(line)
    if plot_list:
        plot_list.append({
            'x': plot_list[0]["x"],
            'y': net_gain,
            'name': 'Net gain',
            'line': {'color': 'rgba(0,0,0,1)'},
            'text': [f'{val:.2f} - net gain' for val in net_gain],
            'hoverinfo': 'x+text',
        })
    return plot_list


def get_leader_dicts(session, most_recent_date):
    rulers = []
    scientists = []
    governors = []
    admirals = []
    generals = []
    assigned_ids = set()
    for leader in session.query(models.Leader).order_by(models.Leader.date_hired).all():
        base_ruler_id = "_".join(leader.leader_name.split()).lower()
        ruler_id = base_ruler_id
        id_offset = 1
        while ruler_id in assigned_ids:
            id_offset += 1
            ruler_id = f"{base_ruler_id}_{id_offset}"
        species = "Unknown"
        if leader.species is not None:
            species = leader.species.species_name
        leader_dict = dict(
            name=leader.leader_name,
            id=f"{ruler_id}",
            in_game_id=leader.leader_id_in_game,
            birthday=models.days_to_date(leader.date_born),
            date_hired=models.days_to_date(leader.date_hired),
            status=f"active (as of {models.days_to_date(most_recent_date)})",
            species=species,
            achievements=[str(a) for a in leader.achievements]
        )
        if leader.last_date < most_recent_date - 720:
            random.seed(leader.leader_name)
            leader_dict["status"] = f"dismissed or deceased around {models.days_to_date(leader.last_date + random.randint(0, 30))}"
        if leader.leader_class == models.LeaderClass.scientist:
            leader_dict["class"] = "Scientist"
            scientists.append(leader_dict)
        elif leader.leader_class == models.LeaderClass.governor:
            leader_dict["class"] = "Governor"
            governors.append(leader_dict)
        elif leader.leader_class == models.LeaderClass.admiral:
            leader_dict["class"] = "Admiral"
            admirals.append(leader_dict)
        elif leader.leader_class == models.LeaderClass.general:
            leader_dict["class"] = "General"
            generals.append(leader_dict)
        elif leader.leader_class == models.LeaderClass.ruler:
            leader_dict["class"] = "Ruler"
            rulers.append(leader_dict)

    leaders = (
            rulers
            + scientists
            + governors
            + admirals
            + generals
    )
    return leaders


def get_war_dicts(session, current_date):
    wars = []
    for war in session.query(models.War).order_by(models.War.start_date_days).all():
        start = models.days_to_date(war.start_date_days)
        end = models.days_to_date(current_date)
        if war.end_date_days:
            end = models.days_to_date(war.end_date_days)

        attackers = [
            f'{wp.country.country_name}: "{wp.war_goal}" war goal' for wp in war.participants
            if wp.is_attacker
        ]
        defenders = [
            f'{wp.country.country_name}: "{wp.war_goal}" war goal' for wp in war.participants
            if not wp.is_attacker
        ]

        victories = sorted([we for wp in war.participants for we in wp.victories], key=lambda we: we.date)
        war_id = "_".join(war.name.split()).lower()
        wars.append(dict(
            name=war.name,
            id=war_id,
            start=start,
            end=end,
            attackers=attackers,
            defenders=defenders,
            combat=[str(vic) for vic in victories],
        ))

    return wars


def start_server():
    timeline_app.run_server(port=config.CONFIG.port)


if __name__ == '__main__':
    start_server()
