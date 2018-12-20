import itertools
import logging
import random
import time
from typing import Dict, Any, Union

from stellarisdashboard import models, game_info, config

logger = logging.getLogger(__name__)


# to keep pycharm happy:
# noinspection PyArgumentList
class TimelineExtractor:
    """
    Processes data from parsed gamestate dictionaries and adds it to the database.
    Only process_gamestate should be called from outside the class.
    """

    # Some constants to represent special pseudo-factions, to categorize pops that are unaffiliated for some reason
    NO_FACTION = "No faction"
    SLAVE_FACTION_NAME = "No faction (enslaved)"
    PURGE_FACTION_NAME = "No faction (purge)"
    NON_SENTIENT_ROBOT_FACTION_NAME = "No faction (non-sentient robot)"
    NO_FACTION_ID = -1
    SLAVE_FACTION_ID = -2
    PURGE_FACTION_ID = -3
    NON_SENTIENT_ROBOT_FACTION_ID = -4

    NO_FACTION_POP_ETHICS = {
        NO_FACTION: "no ethics",
        SLAVE_FACTION_NAME: "no ethics (enslaved)",
        PURGE_FACTION_NAME: "no ethics (purge)",
        NON_SENTIENT_ROBOT_FACTION_NAME: "no ethics (robot)",
    }

    NO_FACTION_ID_MAP = {
        NO_FACTION: NO_FACTION_ID,
        SLAVE_FACTION_NAME: SLAVE_FACTION_ID,
        PURGE_FACTION_NAME: PURGE_FACTION_ID,
        NON_SENTIENT_ROBOT_FACTION_NAME: NON_SENTIENT_ROBOT_FACTION_ID,
    }

    # TODO consider adding more types,  "ruined_marauders", "dormant_marauders", "awakened_marauders"
    SUPPORTED_COUNTRY_TYPES = {"default", "fallen_empire", "awakened_fallen_empire"}

    def __init__(self):
        self._gamestate_dict = None
        self.game = None
        self._session = None
        self._player_country_id: int = None
        self._current_gamestate = None
        self._species_by_ingame_id = None
        self._robot_species = None
        self._systems_by_ingame_country_id = None
        self._planets_by_ingame_country_id = None
        self._country_by_ingame_planet_id = None
        self._country_by_faction_id = None
        self._player_research_agreements = None
        self._player_sensor_links = None
        self._player_monthly_trade_info = None
        self._date_in_days = None
        self._logger_str = None

        self._random_instance = random.Random()

        self._new_models = []
        self._enclave_trade_modifiers = None
        self._initialize_enclave_trade_info()

    def process_gamestate(self, game_name: str, gamestate_dict: Dict[str, Any]):
        """
        This is the only method that should be called. A database session is created for the
        game identified by the game_name, and the relevant data is extracted from the gamestate_dict.

        :param game_name: The game name used to identify the correct database. (e.g. "earthcustodianship_-1585140336")
        :param gamestate_dict: A dictionary returned by the save file parser.
        :return:
        """
        self._random_instance.seed(game_name)
        date_str = gamestate_dict["date"]
        self._logger_str = f"{game_name} {date_str}:"
        logger.info(f"Processing {game_name}, {date_str}")
        self._gamestate_dict = gamestate_dict
        if len({player["country"] for player in self._gamestate_dict["player"]}) != 1:
            logger.warning(f"{self._logger_str} Player country is ambiguous!")
            return None
        self._player_country_id = self._gamestate_dict["player"][0]["country"]
        with models.get_db_session(game_name) as self._session:
            try:
                self.game = self._session.query(models.Game).filter_by(game_name=game_name).first()
                if self.game is None:
                    logger.info(f"Adding new game {game_name} to database.")
                    player_country_name = self._gamestate_dict["country"][self._player_country_id]["name"]
                    self.game = models.Game(game_name=game_name, player_country_name=player_country_name)
                    self._session.add(self.game)
                    self._extract_galaxy_data()
                self._date_in_days = models.date_to_days(self._gamestate_dict["date"])
                game_states_by_date = {gs.date: gs for gs in self.game.game_states}
                if self._date_in_days in game_states_by_date:
                    logger.info(f"Gamestate for {self.game.game_name}, date {date_str} exists! Replacing...")
                    self._session.delete(game_states_by_date[self._date_in_days])
                self._process_gamestate()
                self._session.commit()
                logger.info(f"{self._logger_str} Committed changes to database.")
            except Exception as e:
                self._session.rollback()
                logger.error(e)
                if config.CONFIG.debug_mode or isinstance(e, KeyboardInterrupt):
                    raise e
            finally:
                self._reset_state()

    def _extract_galaxy_data(self):
        logger.info(f"{self._logger_str} Extracting galaxy data...")
        for system_id_in_game in self._gamestate_dict.get("galactic_object", {}):
            self._add_single_system(system_id_in_game)

    def _add_single_system(self, system_id: int, country_model: models.Country = None) -> Union[models.System, None]:
        system_data = self._gamestate_dict.get("galactic_object", {}).get(system_id)
        if system_data is None:
            logger.warning(f"{self._logger_str} Found no data for system with ID {system_id}!")
            return
        original_system_name = system_data.get("name")
        coordinate_x = system_data.get("coordinate", {}).get("x", 0)
        coordinate_y = system_data.get("coordinate", {}).get("y", 0)
        system_model = models.System(
            game=self.game,
            system_id_in_game=system_id,
            star_class=system_data.get("star_class", "Unknown"),
            name=original_system_name,
            coordinate_x=coordinate_x,
            coordinate_y=coordinate_y,
        )
        self._session.add(system_model)
        if country_model is not None:
            self._session.add(models.HistoricalEvent(
                event_type=models.HistoricalEventType.discovered_new_system,
                system=system_model,
                country=country_model,
                start_date_days=self._date_in_days,
                end_date_days=self._date_in_days,
                event_is_known_to_player=True,  # galactic map is always visible
            ))

        for hl_data in system_data.get("hyperlane", []):
            neighbor_id = hl_data.get("to")
            if neighbor_id == system_id:
                continue  # This can happen in Stellaris 2.1
            neighbor_model = self._session.query(models.System).filter_by(
                system_id_in_game=neighbor_id
            ).one_or_none()
            if neighbor_model is None:
                continue  # assume that the hyperlane will be created when adding the neighbor system to DB later

            self._session.add(models.HyperLane(
                system_one=system_model,
                system_two=neighbor_model,
            ))
        return system_model

    def _process_gamestate(self):
        self._current_gamestate = models.GameState(
            game=self.game, date=self._date_in_days,
        )
        self._session.add(self._current_gamestate)
        self._extract_player_trade_agreements()
        self._initialize_system_and_planet_ownership_dicts()
        self._extract_species_and_initialize_species_by_id_dict()
        player_country_data = None

        for country_id, country_data_dict in self._gamestate_dict["country"].items():
            if not isinstance(country_data_dict, dict):
                continue
            country_type = country_data_dict.get("type")
            country_model = self._session.query(models.Country).filter_by(
                game=self.game,
                country_id_in_game=country_id
            ).one_or_none()
            if country_model is None:
                country_model = models.Country(
                    is_player=(country_id == self._player_country_id),
                    country_id_in_game=country_id,
                    game=self.game,
                    country_type=country_type,
                    country_name=country_data_dict.get("name", "no name")
                )
                if country_id == self._player_country_id:
                    country_model.first_player_contact_date = 0
                self._session.add(country_model)

            if country_type not in self.SUPPORTED_COUNTRY_TYPES:
                continue  # Skip enclaves, leviathans, etc ....

            self._extract_country_leaders(country_model, country_data_dict)
            diplomacy_data = self._process_diplomacy(country_model, country_data_dict)
            if country_model.first_player_contact_date is None and diplomacy_data.get("has_communications_with_player"):
                country_model.first_player_contact_date = self._date_in_days
                self._session.add(country_model)

            self._extract_country_government(country_model, country_data_dict)

            country_data_model = self._extract_country_data(country_id, country_model, country_data_dict, diplomacy_data)
            self._extract_country_economy(country_data_model, country_data_dict)
            if country_model.is_player:
                player_country_data = country_data_model
            if country_data_model.attitude_towards_player.is_known():
                debug_name = country_data_dict.get('name', 'Unnamed Country')
                logger.info(f"{self._logger_str} Extracting country info: {debug_name}")

            if config.CONFIG.only_read_player_history and not country_model.is_player:
                continue
            self._history_process_planet_and_sector_events(country_model, country_data_dict)

            self._extract_factions_and_faction_leaders(country_model)
            self._history_add_ruler_events(country_model, country_data_dict)
            self._history_add_tech_events(country_model, country_data_dict)

        self._extract_wars()
        self._settle_finished_wars()
        if config.CONFIG.extract_system_ownership:
            self._extract_system_ownership()
        self._process_pop_data(player_country_data)

    def _extract_country_government(self, country_model: models.Country, country_dict):
        gov_name = country_dict.get("name", "Unnamed Country")
        ethics_list = country_dict.get("ethos", {}).get("ethic", [])
        if not isinstance(ethics_list, list):
            ethics_list = [ethics_list]
        ethics = set(ethics_list)

        gov_dict = country_dict.get("government", {})
        civics_list = gov_dict.get("civics", [])
        if not isinstance(civics_list, list):
            civics_list = [civics_list]
        civics = set(civics_list)
        authority = gov_dict.get("authority", "other")
        gov_type = gov_dict.get("type", "other")
        gov_was_reformed = False

        prev_gov = self._session.query(models.Government).filter(
            models.Government.start_date_days <= self._date_in_days,
        ).filter_by(
            country=country_model,
        ).order_by(
            models.Government.start_date_days.desc()
        ).first()

        if prev_gov is not None:
            prev_gov.end_date_days = self._date_in_days - 1
            self._session.add(prev_gov)
            previous_ethics = [prev_gov.ethics_1, prev_gov.ethics_2, prev_gov.ethics_3, prev_gov.ethics_4, prev_gov.ethics_5]
            previous_ethics = set(previous_ethics) - {None}
            previous_civics = [prev_gov.civic_1, prev_gov.civic_2, prev_gov.civic_3, prev_gov.civic_4, prev_gov.civic_5]
            previous_civics = set(previous_civics) - {None}
            gov_was_reformed = ((ethics != previous_ethics)
                                or (civics != previous_civics)
                                or (gov_name != prev_gov.gov_name)
                                or (gov_type != prev_gov.gov_type))
            # nothing has changed...
            if not gov_was_reformed:
                return

        ethics = dict(zip([f"ethics_{i}" for i in range(1, 6)], sorted(ethics)))
        civics = dict(zip([f"civic_{i}" for i in range(1, 6)], sorted(civics)))

        gov = models.Government(
            country=country_model,
            start_date_days=self._date_in_days - 1,
            end_date_days=self._date_in_days + 1,
            gov_name=gov_name,
            gov_type=gov_type,
            authority=authority,
            personality=country_dict.get("personality", "unknown_personality"),
            **ethics,
            **civics,
        )
        self._session.add(gov)
        if gov_was_reformed:
            ruler = self._get_current_ruler(country_dict)
            self._session.add(models.HistoricalEvent(
                event_type=models.HistoricalEventType.government_reform,
                country=country_model,
                leader=ruler,  # might be none, but probably very unlikely (?)
                start_date_days=self._date_in_days,
                end_date_days=self._date_in_days,
                event_is_known_to_player=country_model.has_met_player(),
            ))

    def _initialize_system_and_planet_ownership_dicts(self):
        self._systems_by_ingame_country_id = {}
        self._planets_by_ingame_country_id = {}
        self._country_by_ingame_planet_id = {}
        for country_id, country_dict in self._gamestate_dict["country"].items():
            if not isinstance(country_dict, dict):
                continue
            self._systems_by_ingame_country_id[country_id] = set()
            self._planets_by_ingame_country_id[country_id] = set(country_dict.get("owned_planets", []))

            for p_id in self._planets_by_ingame_country_id[country_id]:
                self._country_by_ingame_planet_id[p_id] = country_id

            owned_sectors = country_dict.get("owned_sectors", [])
            if not isinstance(owned_sectors, list):
                continue
            for sector_id in owned_sectors:
                sector_dict = self._gamestate_dict["sectors"].get(sector_id)
                if not isinstance(sector_dict, dict):
                    continue
                for system_id_in_game in sector_dict.get("systems", []):
                    if system_id_in_game is None:
                        continue
                    self._systems_by_ingame_country_id[country_id].add(system_id_in_game)

    def _extract_species_and_initialize_species_by_id_dict(self):
        self._species_by_ingame_id = {}
        self._robot_species = set()

        for species_index, species_dict in enumerate(self._gamestate_dict.get("species", [])):
            species_model = self._get_or_add_species(species_index)
            self._species_by_ingame_id[species_index] = species_model
            if species_dict.get("class") == "ROBOT":
                self._robot_species.add(species_index)

    def _process_pop_data(self, country_data: models.CountryData):
        def init_dict():
            return dict(pop_count=0, crime=0, happiness=0, power=0)

        country_id_in_game = country_data.country.country_id_in_game

        stats_by_species = {}
        stats_by_faction = {}
        stats_by_job = {}
        stats_by_stratum = {}
        stats_by_ethos = {}
        stats_by_planet = {}

        for pop_dict in self._gamestate_dict["pop"].values():
            if not isinstance(pop_dict, dict):
                continue
            planet_id = pop_dict.get("planet")
            if self._country_by_ingame_planet_id.get(planet_id) != country_id_in_game:
                # for now, only player pops are processed
                continue
            species_id = pop_dict.get("species_index")
            faction_id = pop_dict.get("pop_faction")

            if faction_id is None:
                if pop_dict.get("enslaved") == "yes":
                    faction_id = self.SLAVE_FACTION_ID
                elif species_id in self._robot_species:
                    faction_id = self.NON_SENTIENT_ROBOT_FACTION_ID
                # TODO: Figure out how to detect Purges
                else:
                    faction_id = self.NO_FACTION_ID

            ethos = pop_dict.get("ethos", {}).get("ethic")
            if not isinstance(ethos, str):
                ethos = "ethic_no_ethos"

            job = pop_dict.get("job", "unemployed")
            stratum = pop_dict.get("category", "unknown stratum")

            crime = pop_dict.get("crime", 0.0)
            happiness = pop_dict.get("happiness", 0.0)
            power = pop_dict.get("power", 0.0)

            if species_id not in stats_by_species:
                stats_by_species[species_id] = init_dict()
            if faction_id not in stats_by_faction:
                stats_by_faction[faction_id] = init_dict()
            if job not in stats_by_job:
                stats_by_job[job] = init_dict()
            if stratum not in stats_by_stratum:
                stats_by_stratum[stratum] = init_dict()
            if ethos not in stats_by_ethos:
                stats_by_ethos[ethos] = init_dict()
            if planet_id not in stats_by_planet:
                stats_by_planet[planet_id] = init_dict()

            stats_by_species[species_id]["pop_count"] += 1
            stats_by_faction[faction_id]["pop_count"] += 1
            stats_by_job[job]["pop_count"] += 1
            stats_by_stratum[stratum]["pop_count"] += 1
            stats_by_ethos[ethos]["pop_count"] += 1
            stats_by_planet[planet_id]["pop_count"] += 1

            stats_by_species[species_id]["crime"] += crime
            stats_by_faction[faction_id]["crime"] += crime
            stats_by_job[job]["crime"] += crime
            stats_by_stratum[stratum]["crime"] += crime
            stats_by_ethos[ethos]["crime"] += crime
            stats_by_planet[planet_id]["crime"] += crime

            stats_by_species[species_id]["happiness"] += happiness
            stats_by_faction[faction_id]["happiness"] += happiness
            stats_by_job[job]["happiness"] += happiness
            stats_by_stratum[stratum]["happiness"] += happiness
            stats_by_ethos[ethos]["happiness"] += happiness
            stats_by_planet[planet_id]["happiness"] += happiness

            stats_by_species[species_id]["power"] += power
            stats_by_faction[faction_id]["power"] += power
            stats_by_job[job]["power"] += power
            stats_by_stratum[stratum]["power"] += power
            stats_by_ethos[ethos]["power"] += power
            stats_by_planet[planet_id]["power"] += power

        for species_id, stats in stats_by_species.items():
            if stats["pop_count"] == 0:
                continue
            stats["crime"] /= stats["pop_count"]
            stats["happiness"] /= stats["pop_count"]
            stats["power"] /= stats["pop_count"]

            species = self._get_or_add_species(species_id)
            self._session.add(models.PopStatsBySpecies(
                country_data=country_data,
                species=species,
                **stats,
            ))

        gamestate_dict_factions = self._gamestate_dict.get("pop_factions", {})
        if not isinstance(gamestate_dict_factions, dict):
            gamestate_dict_factions = {}
        for faction_id, stats in stats_by_faction.items():
            if stats["pop_count"] == 0:
                continue
            faction_dict = gamestate_dict_factions.get(faction_id, {})
            if not isinstance(faction_dict, dict):
                faction_dict = {}
            stats["crime"] /= stats["pop_count"]
            stats["happiness"] /= stats["pop_count"]
            stats["power"] /= stats["pop_count"]
            stats["faction_approval"] = faction_dict.get("faction_approval", 0.0)
            stats["support"] = faction_dict.get("support", 0.0)

            faction = self._session.query(models.PoliticalFaction).filter_by(
                country=country_data.country,
                faction_id_in_game=faction_id,
            ).one_or_none()
            if faction is None:
                continue
            self._session.add(models.PopStatsByFaction(
                country_data=country_data,
                faction=faction,
                **stats,
            ))

        for planet_id, stats in stats_by_planet.items():
            if stats["pop_count"] == 0:
                continue
            stats["crime"] /= stats["pop_count"]
            stats["happiness"] /= stats["pop_count"]
            stats["power"] /= stats["pop_count"]

            planet_dict = self._gamestate_dict["planet"].get(planet_id)
            if not isinstance(planet_dict, dict):
                continue

            stats["migration"] = planet_dict.get("migration", 0.0)
            stats["free_amenities"] = planet_dict.get("free_amenities", 0.0)
            stats["free_housing"] = planet_dict.get("free_housing", 0.0)
            stats["stability"] = planet_dict.get("stability", 0.0)

            planet = self._session.query(models.Planet).filter_by(
                planet_id_in_game=planet_id
            ).one_or_none()
            if planet is None:
                logger.warning(f"{self._logger_str}: Could not find planet with ID {planet_id}!")
                continue
            self._session.add(models.PlanetStats(
                gamestate=self._current_gamestate,
                planet=planet,
                **stats,
            ))

        for job, stats in stats_by_job.items():
            if stats["pop_count"] == 0:
                continue
            stats["crime"] /= stats["pop_count"]
            stats["happiness"] /= stats["pop_count"]
            stats["power"] /= stats["pop_count"]

            job = self._get_or_add_shared_description(job)
            self._session.add(models.PopStatsByJob(
                country_data=country_data,
                db_job_description=job,
                **stats,
            ))

        for stratum, stats in stats_by_stratum.items():
            if stats["pop_count"] == 0:
                continue
            stats["crime"] /= stats["pop_count"]
            stats["happiness"] /= stats["pop_count"]
            stats["power"] /= stats["pop_count"]

            stratum = self._get_or_add_shared_description(stratum)
            self._session.add(models.PopStatsByStratum(
                country_data=country_data,
                db_stratum_description=stratum,
                **stats,
            ))

        for ethos, stats in stats_by_ethos.items():
            if stats["pop_count"] == 0:
                continue
            stats["crime"] /= stats["pop_count"]
            stats["happiness"] /= stats["pop_count"]
            stats["power"] /= stats["pop_count"]

            ethos = self._get_or_add_shared_description(ethos)
            self._session.add(models.PopStatsByEthos(
                country_data=country_data,
                db_ethos_description=ethos,
                **stats,
            ))

    def _extract_country_data(self, country_id, country: models.Country, country_dict, diplomacy_data) -> models.CountryData:
        is_player = (country_id == self._player_country_id)
        has_research_agreement_with_player = is_player or (country_id in self._player_research_agreements)

        has_sensor_link_with_player = is_player or (country_id in self._player_sensor_links)
        if is_player:
            attitude_towards_player = models.Attitude.is_player
        else:
            attitude_towards_player = self._extract_ai_attitude_towards_player(country_dict)

        tech_count = len(country_dict.get("tech_status", {}).get("technology", []))
        country_data = models.CountryData(
            date=self._date_in_days,
            country=country,
            game_state=self._current_gamestate,

            military_power=country_dict.get("military_power", 0),
            tech_power=country_dict.get("tech_power", 0),
            fleet_size=country_dict.get("fleet_size", 0),
            empire_size=country_dict.get("empire_size", 0),
            empire_cohesion=country_dict.get("empire_cohesion", 0),
            tech_count=tech_count,
            exploration_progress=len(country_dict.get("surveyed", 0)),
            owned_planets=len(country_dict.get("owned_planets", [])),
            controlled_systems=len(self._systems_by_ingame_country_id.get(country_id, [])),

            victory_rank=country_dict.get("victory_rank", 0),
            victory_score=country_dict.get("victory_score", 0),
            economy_power=country_dict.get("economy_power", 0),

            has_research_agreement_with_player=has_research_agreement_with_player,
            has_sensor_link_with_player=has_sensor_link_with_player,
            attitude_towards_player=attitude_towards_player,

            # Resource income values are calculated in _extract_country_economy
            net_energy=0.0,
            net_minerals=0.0,
            net_alloys=0.0,
            net_consumer_goods=0.0,
            net_food=0.0,
            net_unity=0.0,
            net_influence=0.0,
            net_physics_research=0.0,
            net_society_research=0.0,
            net_engineering_research=0.0,

            **diplomacy_data,
        )
        self._session.add(country_data)
        return country_data

    def _extract_country_economy(self, country_data: models.CountryData, country_dict):
        budget_dict = country_dict.get("budget", {}).get("current_month", {}).get("balance", {})

        for item_name, values in budget_dict.items():
            if item_name == "none":
                continue
            if not values:
                continue
            energy = values.get("energy", 0.0)
            minerals = values.get("minerals", 0.0)
            alloys = values.get("alloys", 0.0)
            consumer_goods = values.get("consumer_goods", 0.0)
            food = values.get("food", 0.0)
            unity = values.get("unity", 0.0)
            influence = values.get("influence", 0.0)
            physics = values.get("physics_research", 0.0)
            society = values.get("society_research", 0.0)
            engineering = values.get("engineering_research", 0.0)

            country_data.net_energy += energy
            country_data.net_minerals += minerals
            country_data.net_alloys += alloys
            country_data.net_consumer_goods += consumer_goods
            country_data.net_food += food
            country_data.net_unity += unity
            country_data.net_influence += influence
            country_data.net_physics_research += physics
            country_data.net_society_research += society
            country_data.net_engineering_research += engineering

            if country_data.country.is_player:
                description = self._get_or_add_shared_description(item_name)

                self._session.add(models.BudgetItem(
                    country_data=country_data,
                    db_budget_item_name=description,
                    net_energy=energy,
                    net_minerals=minerals,
                    net_food=food,
                    net_alloys=alloys,
                    net_consumer_goods=consumer_goods,
                    net_unity=unity,
                    net_influence=influence,
                    net_volatile_motes=values.get("volatile_motes", 0.0),
                    net_exotic_gases=values.get("exotic_gases", 0.0),
                    net_rare_crystals=values.get("rare_crystals", 0.0),
                    net_living_metal=values.get("living_metal", 0.0),
                    net_zro=values.get("zro", 0.0),
                    net_dark_matter=values.get("dark_matter", 0.0),
                    net_nanites=values.get("nanites", 0.0),
                    net_physics_research=physics,
                    net_society_research=society,
                    net_engineering_research=engineering,
                ))
        self._session.add(country_data)  # update

    def _process_diplomacy(self, country_model: models.Country, country_dict):
        relations_manager = country_dict.get("relations_manager", [])
        diplomacy_towards_player = dict(
            has_rivalry_with_player=False,
            has_defensive_pact_with_player=False,
            has_federation_with_player=False,
            has_non_aggression_pact_with_player=False,
            has_closed_borders_with_player=False,
            has_communications_with_player=False,
            has_migration_treaty_with_player=False,
            has_commercial_pact_with_player=False,
            is_player_neighbor=False,
        )
        if not isinstance(relations_manager, dict):
            return diplomacy_towards_player
        relation_list = relations_manager.get("relation", [])
        if not isinstance(relation_list, list):  # if there is only one
            relation_list = [relation_list]
        for relation in relation_list:
            if not isinstance(relation, dict):
                continue

            if not config.CONFIG.only_read_player_history or country_model.is_player:
                self._history_add_or_update_diplomatic_events(country_model, country_dict, relation)

            if relation.get("country") == self._player_country_id:
                diplomacy_towards_player.update(
                    has_rivalry_with_player=relation.get("is_rival") == "yes",
                    has_defensive_pact_with_player=relation.get("defensive_pact") == "yes",
                    has_federation_with_player=relation.get("alliance") == "yes",
                    has_non_aggression_pact_with_player=relation.get("non_aggression_pledge") == "yes",
                    has_closed_borders_with_player=relation.get("closed_borders") == "yes",
                    has_communications_with_player=relation.get("communications") == "yes",
                    has_migration_treaty_with_player=relation.get("migration_access") == "yes",
                    is_player_neighbor=relation.get("borders") == "yes",
                )
            break
        return diplomacy_towards_player

    def _history_add_or_update_diplomatic_events(self, country_model: models.Country, country_dict, relation):
        relation_country = relation.get("country")
        target_country_model = self._session.query(models.Country).filter_by(
            country_id_in_game=relation_country,
        ).one_or_none()
        if target_country_model is None:
            return  # target country might not be in DB yet if this is the first save...
        ruler = self._get_current_ruler(country_dict)
        target_country_dict = self._gamestate_dict["country"].get(target_country_model.country_id_in_game)
        tc_ruler = None
        if target_country_dict.get("country_type") in self.SUPPORTED_COUNTRY_TYPES:
            tc_ruler = self._get_current_ruler(target_country_dict)

        is_known_to_player = country_model.has_met_player() and target_country_model.has_met_player()
        diplo_relations = [
            (
                models.HistoricalEventType.sent_rivalry,
                models.HistoricalEventType.received_rivalry,
                relation.get("is_rival") == "yes"
            ),
            (
                models.HistoricalEventType.closed_borders,
                models.HistoricalEventType.received_closed_borders,
                relation.get("closed_borders") == "yes"
            ),
            (
                models.HistoricalEventType.defensive_pact,
                models.HistoricalEventType.defensive_pact,
                relation.get("defensive_pact") == "yes"
            ),
            (
                models.HistoricalEventType.formed_federation,
                models.HistoricalEventType.formed_federation,
                relation.get("alliance") == "yes"
            ),
            (
                models.HistoricalEventType.non_aggression_pact,
                models.HistoricalEventType.non_aggression_pact,
                relation.get("non_aggression_pledge") == "yes"
            ),
            (
                models.HistoricalEventType.first_contact,
                models.HistoricalEventType.first_contact,
                relation.get("communications") == "yes"
            ),
            (
                models.HistoricalEventType.commercial_pact,
                models.HistoricalEventType.commercial_pact,
                relation.get("commercial_pact") == "yes"
            ),
        ]
        for event_type, reverse_event_type, relation_status in diplo_relations:
            if relation_status:
                country_tuples = [
                    (event_type, country_model, target_country_model, ruler),
                    (reverse_event_type, target_country_model, country_model, tc_ruler)
                ]
                for (et, c_model, tc_model, c_ruler) in country_tuples:
                    matching_event = self._session.query(models.HistoricalEvent).filter_by(
                        event_type=et,
                        country=c_model,
                        target_country=tc_model,
                    ).order_by(models.HistoricalEvent.start_date_days.desc()).first()

                    if matching_event is None or matching_event.end_date_days < self._date_in_days - 5 * 360:
                        matching_event = models.HistoricalEvent(
                            event_type=et,
                            country=c_model,
                            target_country=tc_model,
                            leader=c_ruler,
                            start_date_days=self._date_in_days,
                            end_date_days=self._date_in_days,
                            event_is_known_to_player=is_known_to_player,
                        )
                    else:
                        matching_event.end_date_days = self._date_in_days
                        matching_event.is_known_to_player = is_known_to_player
                    self._session.add(matching_event)

    def _extract_enclave_resource_deals(self, country_dict):
        enclave_deals = dict(
            mineral_income_enclaves=0,
            mineral_spending_enclaves=0,
            energy_income_enclaves=0,
            energy_spending_enclaves=0,
            food_income_enclaves=0,
            food_spending_enclaves=0,
        )

        timed_modifier_list = country_dict.get("timed_modifier", [])
        if not isinstance(timed_modifier_list, list):
            # if for some reason there is only a single timed_modifier, timed_modifier_list will not be a list but a dictionary => Put it in a list!
            timed_modifier_list = [timed_modifier_list]
        for modifier_dict in timed_modifier_list:
            if not isinstance(modifier_dict, dict):
                continue
            modifier_id = modifier_dict.get("modifier", "")
            enclave_trade_budget_dict = self._enclave_trade_modifiers.get(modifier_id, {})
            for budget_item, amount in enclave_trade_budget_dict.items():
                enclave_deals[budget_item] += amount
        # Make spending numbers negative:
        enclave_deals["mineral_spending_enclaves"] *= -1
        enclave_deals["energy_spending_enclaves"] *= -1
        enclave_deals["food_spending_enclaves"] *= -1
        return enclave_deals

    def _extract_ai_attitude_towards_player(self, country_data):
        attitude_towards_player = "unknown"
        ai = country_data.get("ai", {})
        if isinstance(ai, dict):
            attitudes = ai.get("attitude", [])
            for attitude in attitudes:
                if not isinstance(attitude, dict):
                    continue
                if attitude.get("country") == self._player_country_id:
                    attitude_towards_player = attitude["attitude"]
                    break
            attitude_towards_player = models.Attitude.__members__.get(attitude_towards_player, models.Attitude.unknown)
        return attitude_towards_player

    # TODO Extract research and sensor link agreements between all countries for HistoricalEvents
    def _extract_player_trade_agreements(self):
        self._player_research_agreements = set()
        self._player_sensor_links = set()
        self._player_monthly_trade_info = dict(
            mineral_trade_income=0,
            mineral_trade_spending=0,
            energy_trade_income=0,
            energy_trade_spending=0,
            food_trade_income=0,
            food_trade_spending=0,
        )
        trades = self._gamestate_dict.get("trade_deal", {})
        if not trades:
            return
        for trade_id, trade_deal in trades.items():
            if not isinstance(trade_deal, dict):
                continue  # could be "none"
            first = trade_deal.get("first", {})
            second = trade_deal.get("second", {})
            if first.get("country", -1) != self._player_country_id:
                first, second = second, first  # make it so player is always first party
            if first.get("country", -1) != self._player_country_id:
                continue  # trade doesn't involve player
            if second.get("research_agreement") == "yes":
                self._player_research_agreements.add(second["country"])
            if second.get("sensor_link") == "yes":
                self._player_sensor_links.add(second["country"])

            player_resources = first.get("monthly_resources", {})
            self._player_monthly_trade_info["mineral_trade_spending"] -= player_resources.get("minerals", 0)
            self._player_monthly_trade_info["energy_trade_spending"] -= player_resources.get("energy", 0)
            self._player_monthly_trade_info["food_trade_spending"] -= player_resources.get("food", 0)
            other_resources = second.get("monthly_resources", {})
            self._player_monthly_trade_info["mineral_trade_income"] += other_resources.get("minerals", 0)
            self._player_monthly_trade_info["energy_trade_income"] += other_resources.get("energy", 0)
            self._player_monthly_trade_info["food_trade_income"] += other_resources.get("food", 0)

    def _extract_factions_and_faction_leaders(self, country_model: models.Country):
        for faction_id, faction_dict in self._gamestate_dict.get("pop_factions", {}).items():
            if not faction_dict or not isinstance(faction_dict, dict):
                continue
            if faction_dict.get("country") != country_model.country_id_in_game:
                continue
            faction_name = faction_dict.get("name", "Unnamed faction")
            # If the faction is in the database, get it, otherwise add a new faction
            faction_model = self._get_or_add_faction(
                faction_id_in_game=faction_id,
                faction_name=faction_name,
                country_model=country_model,
                faction_type=faction_dict.get("type"),
            )
            self._history_add_or_update_faction_leader_event(country_model, faction_model, faction_dict)

        for faction_name, faction_id in self.NO_FACTION_ID_MAP.items():
            self._get_or_add_faction(
                faction_id_in_game=faction_id,
                faction_name=faction_name,
                country_model=country_model,
                faction_type=TimelineExtractor.NO_FACTION_POP_ETHICS[faction_name],
            )

    def _get_or_add_faction(self, faction_id_in_game: int,
                            faction_name: str,
                            country_model: models.Country,
                            faction_type: str):
        faction = self._session.query(models.PoliticalFaction).filter_by(
            faction_id_in_game=faction_id_in_game,
            country=country_model,
        ).one_or_none()
        if faction is None:
            faction = models.PoliticalFaction(
                country=country_model,
                faction_name=faction_name,
                faction_id_in_game=faction_id_in_game,
                db_faction_type=self._get_or_add_shared_description(faction_type),
            )
            self._session.add(faction)
            if faction_id_in_game not in TimelineExtractor.NO_FACTION_ID_MAP.values():
                self._session.add(models.HistoricalEvent(
                    event_type=models.HistoricalEventType.new_faction,
                    country=country_model,
                    faction=faction,
                    start_date_days=self._date_in_days,
                    end_date_days=self._date_in_days,
                    event_is_known_to_player=country_model.has_met_player(),
                ))
        return faction

    def _get_or_add_species(self, species_id_in_game: int):
        species_data = self._gamestate_dict["species"][species_id_in_game]
        species_name = species_data.get("name", "Unnamed Species")
        species = self._session.query(models.Species).filter_by(
            game=self.game, species_id_in_game=species_id_in_game
        ).one_or_none()
        if species is None:
            species = models.Species(
                game=self.game,
                species_name=species_name,
                species_class=species_data.get("class", "Unknown Class"),
                species_id_in_game=species_id_in_game,
                parent_species_id_in_game=species_data.get("base", -1),
            )
            self._session.add(species)
            traits_dict = species_data.get("traits", {})
            if isinstance(traits_dict, dict):
                trait_list = traits_dict.get("trait", [])
                if not isinstance(trait_list, list):
                    trait_list = [trait_list]
                for trait in trait_list:
                    self._session.add(models.SpeciesTrait(
                        db_name=self._get_or_add_shared_description(trait),
                        species=species,
                    ))
        return species

    def _extract_wars(self):
        logger.info(f"{self._logger_str} Processing Wars")
        wars_dict = self._gamestate_dict.get("war", {})
        if not wars_dict:
            return

        for war_id, war_dict in wars_dict.items():
            if not isinstance(war_dict, dict):
                continue
            war_name = war_dict.get("name", "Unnamed war")
            war_model = self._session.query(models.War).order_by(models.War.start_date_days.desc()).filter_by(
                game=self.game, name=war_name
            ).first()
            if war_model is None or (war_model.outcome != models.WarOutcome.in_progress
                                     and war_model.end_date_days < self._date_in_days - 5 * 360):
                start_date_days = models.date_to_days(war_dict["start_date"])
                war_model = models.War(
                    war_id_in_game=war_id,
                    game=self.game,
                    start_date_days=start_date_days,
                    end_date_days=self._date_in_days,
                    name=war_name,
                    outcome=models.WarOutcome.in_progress,
                )
            elif war_dict.get("defender_force_peace") == "yes":
                war_model.outcome = models.WarOutcome.status_quo
                war_model.end_date_days = models.date_to_days(war_dict.get("defender_force_peace_date"))
            elif war_dict.get("attacker_force_peace") == "yes":
                war_model.outcome = models.WarOutcome.status_quo
                war_model.end_date_days = models.date_to_days(war_dict.get("attacker_force_peace_date"))
            elif war_model.outcome != models.WarOutcome.in_progress:
                continue
            else:
                war_model.end_date_days = self._date_in_days
            self._session.add(war_model)
            war_goal_attacker = war_dict.get("attacker_war_goal", {}).get("type")
            war_goal_defender = war_dict.get("defender_war_goal", {})
            if isinstance(war_goal_defender, dict):
                war_goal_defender = war_goal_defender.get("type")
            elif not war_goal_defender or war_goal_defender == "none":
                war_goal_defender = None

            attackers = {p["country"] for p in war_dict["attackers"]}
            for war_party_info in itertools.chain(war_dict["attackers"], war_dict["defenders"]):
                if not isinstance(war_party_info, dict):
                    continue  # just in case
                country_id = war_party_info.get("country")
                db_country = self._session.query(models.Country).filter_by(game=self.game, country_id_in_game=country_id).one_or_none()

                country_dict = self._gamestate_dict["country"][country_id]
                if db_country is None:
                    country_name = country_dict["name"]
                    logger.warning(f"Could not find country matching war participant {country_name}")
                    continue

                is_attacker = country_id in attackers

                war_participant = self._session.query(models.WarParticipant).filter_by(
                    war=war_model, country=db_country
                ).one_or_none()
                if war_participant is None:
                    war_goal = war_goal_attacker if is_attacker else war_goal_defender
                    war_participant = models.WarParticipant(
                        war=war_model,
                        war_goal=war_goal,
                        country=db_country,
                        is_attacker=is_attacker,
                    )
                    self._session.add(models.HistoricalEvent(
                        event_type=models.HistoricalEventType.war,
                        country=war_participant.country,
                        leader=self._get_current_ruler(country_dict),
                        start_date_days=self._date_in_days,
                        end_date_days=self._date_in_days,
                        war=war_model,
                        event_is_known_to_player=war_participant.country.has_met_player(),
                    ))
                if war_participant.war_goal is None:
                    war_participant.war_goal = war_goal_defender
                self._session.add(war_participant)

            self._extract_combat_victories(war_dict, war_model)

    def _extract_combat_victories(self, war_dict, war: models.War):
        battles = war_dict.get("battles", [])
        if not isinstance(battles, list):
            battles = [battles]
        for b_dict in battles:
            if not isinstance(b_dict, dict):
                continue
            battle_attackers = b_dict.get("attackers")
            battle_defenders = b_dict.get("defenders")
            if not battle_attackers or not battle_defenders:
                continue
            if b_dict.get("attacker_victory") not in {"yes", "no"}:
                continue
            attacker_victory = b_dict.get("attacker_victory") == "yes"

            planet_model = self._session.query(models.Planet).filter_by(
                planet_id_in_game=b_dict.get("planet"),
            ).one_or_none()

            if planet_model is None:
                system_id_in_game = b_dict.get("system")
                system = self._session.query(models.System).filter_by(
                    system_id_in_game=system_id_in_game
                ).one_or_none()
                if system is None:
                    system = self._add_single_system(system_id_in_game)
            else:
                system = planet_model.system

            combat_type = models.CombatType.__members__.get(b_dict.get("type"), models.CombatType.other)

            date_str = b_dict.get("date")
            date_in_days = models.date_to_days(date_str)
            if date_in_days < 0:
                date_in_days = self._date_in_days

            attacker_exhaustion = b_dict.get("attacker_war_exhaustion", 0.0)
            defender_exhaustion = b_dict.get("defender_war_exhaustion", 0.0)
            if defender_exhaustion + attacker_exhaustion <= 0.001 and combat_type != models.CombatType.armies:
                continue
            combat = self._session.query(models.Combat).filter_by(
                war=war,
                system=system if system is not None else planet_model.system,
                planet=planet_model,
                combat_type=combat_type,
                attacker_victory=attacker_victory,
                attacker_war_exhaustion=attacker_exhaustion,
                defender_war_exhaustion=defender_exhaustion,
            ).order_by(models.Combat.date.desc()).first()

            if combat is not None:
                continue

            combat = models.Combat(
                war=war,
                date=date_in_days,
                attacker_war_exhaustion=attacker_exhaustion,
                defender_war_exhaustion=defender_exhaustion,
                system=system,
                planet=planet_model,
                combat_type=combat_type,
                attacker_victory=attacker_victory,
            )
            self._session.add(combat)

            is_known_to_player = False
            for country_id in itertools.chain(battle_attackers, battle_defenders):
                db_country = self._session.query(models.Country).filter_by(country_id_in_game=country_id).one_or_none()
                if db_country is None:
                    logger.warning(f"Could not find country with ID {country_id} when processing battle {b_dict}")
                    continue
                is_known_to_player = is_known_to_player or db_country.has_met_player()
                war_participant = self._session.query(models.WarParticipant).filter_by(
                    war=war,
                    country=db_country,
                ).one_or_none()
                if war_participant is None:
                    logger.info(f"Could not find War participant matching country {db_country.country_name} and war {war.name}.")
                    continue
                self._session.add(models.CombatParticipant(
                    combat=combat, war_participant=war_participant, is_attacker=country_id in battle_attackers,
                ))

            event_type = models.HistoricalEventType.army_combat if combat_type == models.CombatType.armies else models.HistoricalEventType.fleet_combat
            self._session.add(models.HistoricalEvent(
                event_type=event_type,
                system=system,
                planet=planet_model,
                war=war,
                start_date_days=date_in_days,
                event_is_known_to_player=is_known_to_player,
            ))

    def _extract_country_leaders(self, country_model: models.Country, country_dict):
        owned_leaders = country_dict.get("owned_leaders", [])
        if not isinstance(owned_leaders, list):  # if there is only one
            owned_leaders = [owned_leaders]
        leaders = self._gamestate_dict["leaders"]
        active_leaders = set(owned_leaders)

        # first, check if the known leaders in the DB are still there
        for leader in self._session.query(models.Leader).filter_by(
                country=country_model,
                is_active=True,
        ).all():
            if leader.leader_id_in_game not in active_leaders:
                leader.is_active = False
            else:
                current_leader_name = self.get_leader_name(leaders.get(leader.leader_id_in_game))
                leader.is_active = (current_leader_name == leader.leader_name
                                    or leader.last_date >= self._date_in_days - 3 * 360)
            if not leader.is_active:
                country_data = country_model.get_most_recent_data()
                self._session.add(models.HistoricalEvent(
                    event_type=models.HistoricalEventType.leader_died,
                    country=country_model,
                    leader=leader,
                    start_date_days=leader.last_date,
                    end_date_days=leader.last_date,
                    event_is_known_to_player=(country_data is not None
                                              and country_data.attitude_towards_player.reveals_economy_info()),
                ))
            self._session.add(leader)

        # then, check for changes in leaders
        for leader_id in owned_leaders:
            leader_dict = leaders.get(leader_id)
            if not isinstance(leader_dict, dict):
                continue
            leader = self._session.query(models.Leader).filter_by(game=self.game, leader_id_in_game=leader_id).one_or_none()
            if leader is None:
                leader = self._add_new_leader(country_model, leader_id, leader_dict)
            leader.is_active = True
            leader.last_date = self._date_in_days
            level = leader_dict.get("level", -1)
            if leader.last_level < level:
                self._session.add(models.HistoricalEvent(
                    event_type=models.HistoricalEventType.level_up,
                    country=country_model,
                    start_date_days=self._date_in_days,
                    leader=leader,
                    event_is_known_to_player=country_model.is_player,
                    db_description=self._get_or_add_shared_description(str(level)),
                ))
                leader.last_level = level
            self._session.add(leader)

    def _add_new_leader(self, country_model: models.Country, leader_id: int, leader_dict) -> models.Leader:
        if "pre_ruler_class" in leader_dict:
            leader_class = leader_dict.get("pre_ruler_class", "Unknown class")
        else:
            leader_class = leader_dict.get("class", "Unknown class")
        leader_gender = leader_dict.get("gender", "Other")
        leader_agenda = leader_dict.get("agenda")
        leader_name = self.get_leader_name(leader_dict)

        date_hired = min(
            self._date_in_days,
            models.date_to_days(leader_dict.get("date", "10000.01.01")),
            models.date_to_days(leader_dict.get("start", "10000.01.01")),
            models.date_to_days(leader_dict.get("date_added", "10000.01.01")),
        )
        date_born = date_hired - 360 * leader_dict.get("age", 0.0) + self._random_instance.randint(-15, 15)
        species_id = leader_dict.get("species_index", -1)
        species = self._get_or_add_species(species_id)
        leader = models.Leader(
            country=country_model,
            leader_id_in_game=leader_id,
            leader_class=leader_class,
            leader_name=leader_name,
            leader_agenda=leader_agenda,
            species=species,
            game=self.game,
            last_level=leader_dict.get("level", 0),
            gender=leader_gender,
            date_hired=date_hired,
            date_born=date_born,
            is_active=True,
        )
        self._session.add(leader)
        country_data = country_model.get_most_recent_data()
        event = models.HistoricalEvent(
            event_type=models.HistoricalEventType.leader_recruited,
            country=country_model,
            leader=leader,
            start_date_days=date_hired,
            end_date_days=self._date_in_days,
            event_is_known_to_player=country_data is not None and country_data.attitude_towards_player.reveals_economy_info(),
        )
        self._session.add(event)
        return leader

    def get_leader_name(self, leader_dict):
        first_name = leader_dict['name']['first_name']
        last_name = leader_dict['name'].get('second_name', "")
        leader_name = f"{first_name} {last_name}".strip()
        return leader_name

    def _history_add_tech_events(self, country_model: models.Country, country_dict):
        tech_status_dict = country_dict.get("tech_status")
        if not isinstance(tech_status_dict, dict):
            return
        country_data = country_model.get_most_recent_data()
        for tech_type in ["physics", "society", "engineering"]:
            scientist_id = tech_status_dict.get("leaders", {}).get(tech_type)
            scientist = self._session.query(models.Leader).filter_by(leader_id_in_game=scientist_id).one_or_none()
            self.history_add_was_research_leader_events(country_model, country_data, scientist, tech_type)

            progress_dict = tech_status_dict.get(f"{tech_type}_queue")
            if progress_dict and isinstance(progress_dict, list):
                progress_dict = progress_dict[0]
            if not isinstance(progress_dict, dict):
                continue
            tech_name = progress_dict.get("technology")
            if not isinstance(tech_name, str):
                continue
            matching_description = self._get_or_add_shared_description(text=tech_name)
            # TODO CHECK IF THIS WORKS FOR REPEATABLE TECH
            # check for existing event in database:
            matching_event = self._session.query(models.HistoricalEvent).filter_by(
                event_type=models.HistoricalEventType.researched_technology,
                country=country_model,
                db_description=matching_description,
            ).one_or_none()
            if matching_event is None:
                date_str = progress_dict.get("date")
                start_date = models.date_to_days(progress_dict.get("date")) if date_str else self._date_in_days
                matching_event = models.HistoricalEvent(
                    event_type=models.HistoricalEventType.researched_technology,
                    country=country_model,
                    leader=scientist,
                    start_date_days=start_date,
                    end_date_days=self._date_in_days,
                    db_description=matching_description,
                    event_is_known_to_player=country_data is not None and country_data.attitude_towards_player.reveals_technology_info(),
                )
            else:
                matching_event.end_date_days = self._date_in_days
            self._session.add(matching_event)

    def history_add_was_research_leader_events(self, country_model: models.Country,
                                               country_data: models.CountryData,
                                               scientist: models.Leader,
                                               tech_type: str):
        """ Record which scientist was in charge of leading research for a given tech type. """
        if scientist is None:
            return

        description = self._get_or_add_shared_description(text=tech_type.capitalize())
        matching_event = self._session.query(models.HistoricalEvent).filter_by(
            event_type=models.HistoricalEventType.research_leader,
            country=country_model,
            db_description=description,
        ).order_by(models.HistoricalEvent.start_date_days.desc()).first()
        if matching_event is None:
            is_known_to_player = country_data is not None and country_data.attitude_towards_player.reveals_technology_info()
            new_event = models.HistoricalEvent(
                event_type=models.HistoricalEventType.research_leader,
                country=country_model,
                leader=scientist,
                start_date_days=self._date_in_days,
                end_date_days=self._date_in_days,
                db_description=description,
                event_is_known_to_player=is_known_to_player,
            )
            self._session.add(new_event)
        elif matching_event.leader == scientist:
            matching_event.end_date_days = self._date_in_days
            self._session.add(matching_event)

    def _history_add_ruler_events(self, country_model: models.Country, country_dict):
        ruler = self._get_current_ruler(country_dict)
        if ruler is not None:
            capital_planet = self._history_add_or_update_capital(country_model, ruler, country_dict)
            self._history_add_or_update_ruler(ruler, country_model, capital_planet)
            self._history_extract_tradition_events(ruler, country_model, country_dict)
            self._history_extract_ascension_events(ruler, country_model, country_dict)
            self._history_extract_edict_events(ruler, country_model, country_dict)

    def _get_current_ruler(self, country_dict) -> Union[models.Leader, None]:
        if not isinstance(country_dict, dict):
            return None
        ruler_id = country_dict.get("ruler", -1)
        if ruler_id < 0:
            logger.warning(f"Could not find leader id for ruler!")
            return None
        leader = self._session.query(models.Leader).filter_by(
            leader_id_in_game=ruler_id,
            is_active=True,
        ).order_by(models.Leader.is_active.desc()).first()
        if leader is None:
            logger.warning(f"Could not find leader matching leader id {ruler_id} for country {country_dict.get('name')}")
        return leader

    def _history_add_or_update_capital(self, country_model: models.Country,
                                       ruler: models.Leader,
                                       country_dict) -> models.Planet:
        capital_id = country_dict.get("capital")
        capital = None
        if isinstance(capital_id, int):
            capital = self._session.query(models.Planet).filter_by(
                planet_id_in_game=capital_id
            ).one_or_none()
        capital_event = self._session.query(models.HistoricalEvent).filter_by(
            event_type=models.HistoricalEventType.capital_relocation,
            country=country_model,
        ).order_by(
            models.HistoricalEvent.start_date_days.desc()
        ).first()
        if capital_event is None or capital_event.planet.planet_id != capital.planet_id:
            self._session.add(models.HistoricalEvent(
                event_type=models.HistoricalEventType.capital_relocation,
                country=country_model,
                leader=ruler,
                start_date_days=self._date_in_days,
                planet=capital,
                system=capital.system if capital else None,
                event_is_known_to_player=country_model.has_met_player(),
            ))
        return capital

    def _history_add_or_update_ruler(self, ruler: models.Leader, country_model: models.Country, capital_planet: models.Planet):
        most_recent_ruler_event = self._session.query(models.HistoricalEvent).filter_by(
            event_type=models.HistoricalEventType.ruled_empire,
            country=country_model,
            leader=ruler,
        ).order_by(
            models.HistoricalEvent.start_date_days.desc()
        ).first()
        capital_system = capital_planet.system if capital_planet else None
        if most_recent_ruler_event is None:
            start_date = self._date_in_days
            if start_date < 100:
                start_date = 0
            most_recent_ruler_event = models.HistoricalEvent(
                event_type=models.HistoricalEventType.ruled_empire,
                country=country_model,
                leader=ruler,
                start_date_days=start_date,
                planet=capital_planet,
                system=capital_system,
                end_date_days=self._date_in_days,
                event_is_known_to_player=country_model.has_met_player(),
            )
        else:
            most_recent_ruler_event.end_date_days = self._date_in_days - 1
            most_recent_ruler_event.is_known_to_player = country_model.has_met_player()
        if most_recent_ruler_event.planet is None:
            most_recent_ruler_event.planet = capital_planet
            most_recent_ruler_event.system = capital_system
        self._session.add(most_recent_ruler_event)

    def _history_extract_tradition_events(self, ruler: models.Leader, country_model: models.Country, country_dict):
        for tradition in country_dict.get("traditions", []):
            matching_description = self._get_or_add_shared_description(text=tradition)
            matching_event = self._session.query(models.HistoricalEvent).filter_by(
                country=country_model,
                event_type=models.HistoricalEventType.tradition,
                db_description=matching_description,
            ).one_or_none()
            if matching_event is None:
                country_data = country_model.get_most_recent_data()
                self._session.add(models.HistoricalEvent(
                    leader=ruler,
                    country=country_model,
                    event_type=models.HistoricalEventType.tradition,
                    start_date_days=self._date_in_days,
                    end_date_days=self._date_in_days,
                    db_description=matching_description,
                    event_is_known_to_player=country_data is not None and country_data.attitude_towards_player.reveals_economy_info(),
                ))

    def _history_extract_ascension_events(self, ruler: models.Leader, country_model: models.Country, country_dict):
        for ascension_perk in country_dict.get("ascension_perks", []):
            matching_description = self._get_or_add_shared_description(text=ascension_perk)
            matching_event = self._session.query(models.HistoricalEvent).filter_by(
                country=country_model,
                event_type=models.HistoricalEventType.ascension_perk,
                db_description=matching_description,
            ).one_or_none()
            if matching_event is None:
                self._session.add(models.HistoricalEvent(
                    leader=ruler,
                    country=country_model,
                    event_type=models.HistoricalEventType.ascension_perk,
                    start_date_days=self._date_in_days,
                    end_date_days=self._date_in_days,
                    db_description=matching_description,
                    event_is_known_to_player=country_model.has_met_player(),
                ))

    def _history_extract_edict_events(self, ruler: models.Leader, country_model: models.Country, country_dict):
        edict_list = country_dict.get("edicts", [])
        if not isinstance(edict_list, list):
            edict_list = [edict_list]
        for edict in edict_list:
            if not isinstance(edict, dict):
                continue
            expiry_date = models.date_to_days(edict.get("date"))
            description = self._get_or_add_shared_description(text=(edict.get("edict")))
            matching_event = self._session.query(
                models.HistoricalEvent
            ).filter_by(
                event_type=models.HistoricalEventType.edict,
                country=country_model,
                db_description=description,
                end_date_days=expiry_date,
            ).one_or_none()
            if matching_event is None:
                country_data = country_model.get_most_recent_data()
                self._session.add(models.HistoricalEvent(
                    event_type=models.HistoricalEventType.edict,
                    country=country_model,
                    leader=ruler,
                    db_description=description,
                    start_date_days=self._date_in_days,
                    end_date_days=expiry_date,
                    event_is_known_to_player=country_data is not None and country_data.attitude_towards_player.reveals_economy_info(),
                ))

    def _settle_finished_wars(self):
        truces_dict = self._gamestate_dict.get("truce", {})
        if not isinstance(truces_dict, dict):
            return
        #  resolve wars based on truces...
        for truce_id, truce_info in truces_dict.items():
            if not isinstance(truce_info, dict):
                continue
            war_name = truce_info.get("name")
            truce_type = truce_info.get("truce_type", "other")
            if not war_name or truce_type != "war":
                continue  # truce is due to diplomatic agreements or similar
            matching_war = self._session.query(models.War).order_by(models.War.start_date_days.desc()).filter_by(name=war_name).first()
            if matching_war is None:
                continue
            end_date = truce_info.get("start_date")  # start of truce => end of war
            if isinstance(end_date, str) and end_date != None:
                matching_war.end_date_days = models.date_to_days(end_date)

            if matching_war.outcome == models.WarOutcome.in_progress:
                if matching_war.attacker_war_exhaustion < matching_war.defender_war_exhaustion:
                    matching_war.outcome = models.WarOutcome.attacker_victory
                elif matching_war.defender_war_exhaustion < matching_war.attacker_war_exhaustion:
                    matching_war.outcome = models.WarOutcome.defender_victory
                else:
                    matching_war.outcome = models.WarOutcome.status_quo
                self._history_add_peace_events(matching_war)
            self._session.add(matching_war)
        #  resolve wars that are no longer in the save files...
        for war in self._session.query(models.War).filter_by(outcome=models.WarOutcome.in_progress).all():
            if war.end_date_days < self._date_in_days - 5 * 360:
                war.outcome = models.WarOutcome.unknown
                self._session.add(war)
                self._history_add_peace_events(war)

    def _history_process_planet_and_sector_events(self, country_model, country_dict):
        country_sectors = country_dict.get("owned_sectors", [])
        # processing all colonies by sector allows reading the responsible sector governor
        for sector_id in country_sectors:
            sector_info = self._gamestate_dict["sectors"].get(sector_id)
            if not isinstance(sector_info, dict):
                continue
            sector_description = self._get_or_add_shared_description(text=(sector_info.get("name", "Unnamed")))
            governor_id = sector_info.get("governor")
            governor_model = None
            if governor_id is not None:
                governor_model = self._session.query(models.Leader).filter_by(
                    country=country_model,
                    leader_id_in_game=governor_id,
                ).one_or_none()

            self._history_add_planetary_events_within_sector(country_model, sector_info, governor_model)

            sector_capital = self._session.query(models.Planet).filter_by(
                planet_id_in_game=sector_info.get("local_capital")
            ).one_or_none()
            if governor_model is not None and sector_capital is not None:
                self._history_add_or_update_governor_sector_events(country_model, sector_capital, governor_model, sector_description)

    def _history_add_planetary_events_within_sector(self, country_model: models.Country, sector_dict, governor: models.Leader):
        for system_id in sector_dict.get("systems", []):
            system_model = self._session.query(models.System).filter_by(
                system_id_in_game=system_id,
            ).one_or_none()
            if system_model is None:
                logger.info(f"{self._logger_str}: Adding single system with in-game id {system_id}")
                system_model = self._add_single_system(system_id, country_model=country_model)

            system_dict = self._gamestate_dict["galactic_object"].get(system_id, {})
            if system_model.name != system_dict.get("name"):
                system_model.name = system_dict.get("name")
                self._session.add(system_model)

            planets = system_dict.get("planet", [])
            if not isinstance(planets, list):
                planets = [planets]
            for planet_id in planets:
                planet_dict = self._gamestate_dict["planet"].get(planet_id)
                if not isinstance(planet_dict, dict):
                    continue
                planet_class = planet_dict.get("planet_class")
                is_colonizable = game_info.is_colonizable_planet(planet_class)
                is_destroyed = game_info.is_destroyed_planet(planet_class)
                is_terraformable = is_colonizable or any(m == "terraforming_candidate" for (m, _) in self._all_planetary_modifiers(planet_dict))

                if not (is_colonizable or is_destroyed or is_terraformable):
                    continue  # don't bother with uninteresting planets

                planet_model = self._add_or_update_planet_model(system_model, planet_id)
                if planet_model is None:
                    continue
                if is_colonizable:
                    self._history_add_or_update_colonization_events(country_model, system_model, planet_model, planet_dict, governor)
                    if game_info.is_colonizable_megastructure(planet_class):
                        self._history_add_or_update_habitable_megastructure_construction_event(
                            country_model, system_model, planet_model, planet_dict, governor, system_id
                        )
                if is_terraformable:
                    self._history_add_or_update_terraforming_events(country_model, system_model, planet_model, planet_dict, governor)

    def _history_add_or_update_terraforming_events(self, country_model: models.Country,
                                                   system_model: models.System,
                                                   planet_model: models.Planet,
                                                   planet_dict,
                                                   governor: models.Leader):
        terraform_dict = planet_dict.get("terraform_process")
        if not isinstance(terraform_dict, dict):
            return

        current_pc = planet_dict.get("planet_class")
        target_pc = terraform_dict.get("planet_class")
        text = f"{current_pc},{target_pc}"
        if not game_info.is_colonizable_planet(target_pc):
            logger.info(f"Unexpected target planet class for terraforming of {planet_model.planet_name}: From {planet_model.planet_class} to {target_pc}")
            return
        matching_description = self._get_or_add_shared_description(text)
        matching_event = self._session.query(models.HistoricalEvent).filter_by(
            event_type=models.HistoricalEventType.terraforming,
            db_description=matching_description,
            system=system_model,
            planet=planet_model,
        ).order_by(models.HistoricalEvent.start_date_days.desc()).first()
        if matching_event is None or matching_event.end_date_days < self._date_in_days - 5 * 360:
            matching_event = models.HistoricalEvent(
                event_type=models.HistoricalEventType.terraforming,
                country=country_model,
                system=planet_model.system,
                planet=planet_model,
                leader=governor,
                start_date_days=self._date_in_days,
                end_date_days=self._date_in_days,
                db_description=matching_description,
                event_is_known_to_player=country_model.has_met_player(),
            )
        else:
            matching_event.end_date_days = self._date_in_days
        self._session.add(matching_event)

    def _add_or_update_planet_model(self,
                                    system_model: models.System,
                                    planet_id: int) -> Union[models.Planet, None]:
        planet_dict = self._gamestate_dict["planet"].get(planet_id)
        if not isinstance(planet_dict, dict):
            return None
        planet_class = planet_dict.get("planet_class")
        planet_name = planet_dict.get("name")
        planet_model = self._session.query(models.Planet).filter_by(
            planet_id_in_game=planet_id
        ).one_or_none()
        if planet_model is None:
            colonize_date = planet_dict.get("colonize_date")
            if colonize_date:
                colonize_date = models.date_to_days(colonize_date)
            planet_model = models.Planet(
                planet_name=planet_name,
                planet_id_in_game=planet_id,
                system=system_model,
                planet_class=planet_class,
                colonized_date=colonize_date,
            )
        if planet_model.planet_name != planet_name:
            planet_model.planet_name = planet_name
        if planet_model.planet_class != planet_class:
            planet_model.planet_class = planet_class
        self._session.add(planet_model)
        return planet_model

    def _all_planetary_modifiers(self, planet_dict):
        modifiers = planet_dict.get("timed_modifiers", [])
        if not isinstance(modifiers, list):
            modifiers = [modifiers]
        for m in modifiers:
            if not isinstance(m, dict):
                continue
            modifier = m.get("modifier", "no modifier")
            duration = m.get("days")
            if duration == -1 or not isinstance(duration, int):
                duration = None
            yield modifier, duration

        planet_modifiers = planet_dict.get("planet_modifier", [])
        if not isinstance(planet_modifiers, list):
            planet_modifiers = [planet_modifiers]
        for pm in planet_modifiers:
            if pm is not None:
                yield pm, None

    def _history_add_or_update_colonization_events(self, country_model: models.Country,
                                                   system_model: models.System,
                                                   planet_model: models.Planet,
                                                   planet_dict,
                                                   governor: models.Leader):
        if "colonize_date" in planet_dict or planet_dict.get("pop"):
            # I think one of these occurs once the colonization is finished
            colonization_completed = True
        elif "colonizer_pop" in planet_dict:
            # while colonization still in progress
            colonization_completed = False
        else:
            # planet is not colonized at all
            return

        colonization_end_date = planet_dict.get("colonize_date")
        if not colonization_end_date:
            end_date_days = self._date_in_days
        else:
            end_date_days = models.date_to_days(colonization_end_date)

        if planet_model.colonized_date is not None:
            # abort early if the planet is already added and known to be fully colonized
            return
        elif colonization_completed:
            # set the planet's colonization flag and allow updating the event one last time
            planet_model.colonized_date = colonization_end_date
            self._session.add(planet_model)
        event = self._session.query(models.HistoricalEvent).filter_by(
            event_type=models.HistoricalEventType.colonization,
            planet=planet_model
        ).one_or_none()
        if event is None:
            start_date = self._date_in_days
            if self._date_in_days < 100:
                end_date_days = min(end_date_days, 0)
                if country_model.is_player:
                    end_date_days = 0
                governor = None
            event = models.HistoricalEvent(
                event_type=models.HistoricalEventType.colonization,
                leader=governor,
                country=country_model,
                start_date_days=min(start_date, end_date_days),
                end_date_days=end_date_days,
                planet=planet_model,
                system=system_model,
                event_is_known_to_player=country_model.has_met_player(),
            )
        else:
            event.end_date_days = end_date_days
        self._session.add(event)

    def _history_add_or_update_habitable_megastructure_construction_event(self, country_model: models.Country,
                                                                          system_model: models.System,
                                                                          planet_model: models.Planet,
                                                                          planet_dict,
                                                                          governor: models.Leader,
                                                                          system_id: int):
        planet_class = planet_dict.get("planet_class")
        if planet_class == "pc_ringworld_habitable":
            sys_name = self._gamestate_dict["galactic_object"].get(system_id).get("name", "Unknown system")
            p_name = f"{sys_name} Ringworld"
        elif planet_class == "pc_habitat":
            p_name = planet_dict.get("name")
        else:
            logging.info("Expected megastructre planet class")
            return

        description = self._get_or_add_shared_description(
            text=p_name,
        )
        event = self._session.query(models.HistoricalEvent).filter_by(
            event_type=models.HistoricalEventType.habitat_ringworld_construction,
            system=planet_model.system,
            db_description=description,
        ).one_or_none()
        if event is None:
            start_date = end_date = self._date_in_days  # TODO: change this when tracking the construction sites in the future
            logger.info(f"{self._logger_str}: New Megastructure {models.days_to_date(self._date_in_days)}")
            event = models.HistoricalEvent(
                event_type=models.HistoricalEventType.habitat_ringworld_construction,
                country=country_model,
                leader=governor,
                start_date_days=start_date,
                end_date_days=end_date,
                planet=planet_model,
                system=system_model,
                db_description=description,
                event_is_known_to_player=country_model.has_met_player(),
            )
        elif not event.event_is_known_to_player:
            event.event_is_known_to_player = country_model.has_met_player()
        self._session.add(event)

    def _history_add_or_update_governor_sector_events(self, country_model,
                                                      sector_capital: models.Planet,
                                                      governor: models.Leader,
                                                      sector_description: models.SharedDescription):
        # check if governor was ruling same sector before => update date and return
        event = self._session.query(models.HistoricalEvent).filter_by(
            event_type=models.HistoricalEventType.governed_sector,
            db_description=sector_description,
        ).order_by(models.HistoricalEvent.end_date_days.desc()).first()
        if (event is not None
                and event.leader == governor
                and event.end_date_days > self._date_in_days - 5 * 360):  # if the governor ruled this sector less than 5 years ago, re-use the event...
            event.end_date_days = self._date_in_days
        else:
            country_data = country_model.get_most_recent_data()
            event = models.HistoricalEvent(
                event_type=models.HistoricalEventType.governed_sector,
                leader=governor,
                country=country_model,
                db_description=sector_description,
                start_date_days=self._date_in_days,
                end_date_days=self._date_in_days,
                event_is_known_to_player=country_data is not None and country_data.attitude_towards_player.reveals_economy_info(),
            )

        if event.planet is None and sector_capital is not None:
            event.planet = sector_capital
            event.system = sector_capital.system
        self._session.add(event)

    def _history_add_or_update_faction_leader_event(self,
                                                    country_model: models.Country,
                                                    faction_model: models.PoliticalFaction,
                                                    faction_dict):
        faction_leader_id = faction_dict.get("leader", -1)
        if faction_leader_id < 0:
            return
        leader = self._session.query(models.Leader).filter_by(
            country=country_model,
            leader_id_in_game=faction_leader_id,
        ).one_or_none()
        if leader is None:
            logger.warning(f"Could not find leader matching leader id {faction_leader_id} for {country_model.country_name}\n{faction_dict}")
            return
        matching_event = self._session.query(models.HistoricalEvent).filter_by(
            country=country_model,
            leader=leader,
            event_type=models.HistoricalEventType.faction_leader,
            faction=faction_model,
        ).one_or_none()
        country_data = country_model.get_most_recent_data()
        is_known = country_data is not None and country_data.attitude_towards_player.reveals_demographic_info()
        if matching_event is None:
            matching_event = models.HistoricalEvent(
                country=country_model,
                leader=leader,
                event_type=models.HistoricalEventType.faction_leader,
                faction=faction_model,
                start_date_days=self._date_in_days,
                end_date_days=self._date_in_days,
                event_is_known_to_player=is_known,
            )
        else:
            matching_event.is_known_to_player = is_known
            matching_event.end_date_days = self._date_in_days
        self._session.add(matching_event)

    def _extract_system_ownership(self):
        logger.info(f"{self._logger_str} Processing system ownership")
        start = time.clock()
        starbases = self._gamestate_dict.get("starbases", {})
        if not isinstance(starbases, dict):
            return
        for starbase_dict in starbases.values():
            if not isinstance(starbase_dict, dict):
                continue
            country_id_in_game = starbase_dict.get("owner")
            system_id_in_game = starbase_dict.get("system")
            if system_id_in_game is None or country_id_in_game is None:
                continue
            system = self._session.query(models.System).filter_by(system_id_in_game=system_id_in_game).one_or_none()
            country_model = self._session.query(models.Country).filter_by(country_id_in_game=country_id_in_game).one_or_none()
            if system is None:
                logger.info(f"{self._logger_str}Detected new system {system_id_in_game}!")
                system = self._add_single_system(system_id_in_game, country_model=country_model)
            if country_model is None:
                logger.warning(f"Cannot establish ownership for system {system_id_in_game} and country {country_id_in_game}")
                continue
            ownership = self._session.query(models.SystemOwnership).filter_by(
                system=system
            ).order_by(models.SystemOwnership.end_date_days.desc()).first()
            if ownership is not None:
                ownership.end_date_days = self._date_in_days
                self._session.add(ownership)
            if ownership is None or ownership.country != country_model:
                if ownership is None:
                    event_type = models.HistoricalEventType.expanded_to_system
                    target_country = None
                else:
                    event_type = models.HistoricalEventType.gained_system
                    target_country = ownership.country
                    if target_country is not None:
                        self._session.add(models.HistoricalEvent(
                            event_type=models.HistoricalEventType.lost_system,
                            country=target_country,
                            target_country=country_model,
                            system=system,
                            start_date_days=self._date_in_days,
                            event_is_known_to_player=country_model.has_met_player()
                                                     or (target_country is not None and target_country.has_met_player()),
                        ))
                self._session.add(models.HistoricalEvent(
                    event_type=event_type,
                    country=country_model,
                    target_country=target_country,
                    system=system,
                    start_date_days=self._date_in_days,
                    event_is_known_to_player=country_model.has_met_player()
                                             or (target_country is not None and target_country.has_met_player()),
                ))
                ownership = models.SystemOwnership(
                    start_date_days=self._date_in_days,
                    end_date_days=self._date_in_days + 1,
                    country=country_model,
                    system=system,
                )
                self._session.add(ownership)
        logger.info(f"{self._logger_str} Processed system ownership in {time.clock() - start}s")

    def _history_add_peace_events(self, war: models.War):
        for wp in war.participants:
            matching_event = self._session.query(models.HistoricalEvent).filter_by(
                event_type=models.HistoricalEventType.peace,
                country=wp.country,
                war=war,
            ).one_or_none()
            if matching_event is None:
                self._session.add(models.HistoricalEvent(
                    event_type=models.HistoricalEventType.peace,
                    war=war,
                    country=wp.country,
                    leader=self._get_current_ruler(self._gamestate_dict["country"].get(wp.country.country_id_in_game, {})),
                    start_date_days=war.end_date_days,
                    event_is_known_to_player=wp.country.has_met_player(),
                ))

    def _get_or_add_shared_description(self, text: str) -> models.SharedDescription:
        matching_description = self._session.query(models.SharedDescription).filter_by(
            text=text,
        ).one_or_none()
        if matching_description is None:
            matching_description = models.SharedDescription(text=text)
            self._session.add(matching_description)
        return matching_description

    def _initialize_enclave_trade_info(self):
        """
        Initialize a dictionary representing all possible combinations of:
          - 3 enclave factions
          - 6 resource trade types (pairs of resources, e.g. trade minerals for food)
          - 3 tiers of trading amounts

        The identifiers found in the save files are mapped to a dictionary, which maps
        the type of income/expense to the numeric amount.

        :return:
        """
        trade_level_1 = [10, 20]
        trade_level_2 = [25, 50]
        trade_level_3 = [50, 100]

        trade_energy_for_minerals = ["mineral_income_enclaves", "energy_spending_enclaves"]
        trade_food_for_minerals = ["mineral_income_enclaves", "food_spending_enclaves"]
        trade_minerals_for_energy = ["energy_income_enclaves", "mineral_spending_enclaves"]
        trade_food_for_energy = ["energy_income_enclaves", "food_spending_enclaves"]
        trade_minerals_for_food = ["food_income_enclaves", "mineral_spending_enclaves"]
        trade_energy_for_food = ["food_income_enclaves", "energy_spending_enclaves"]

        self._enclave_trade_modifiers = {
            "enclave_mineral_trade_1_mut": dict(zip(trade_energy_for_minerals, trade_level_1)),
            "enclave_mineral_trade_1_rig": dict(zip(trade_energy_for_minerals, trade_level_1)),
            "enclave_mineral_trade_1_xur": dict(zip(trade_energy_for_minerals, trade_level_1)),
            "enclave_mineral_trade_2_mut": dict(zip(trade_energy_for_minerals, trade_level_2)),
            "enclave_mineral_trade_2_rig": dict(zip(trade_energy_for_minerals, trade_level_2)),
            "enclave_mineral_trade_2_xur": dict(zip(trade_energy_for_minerals, trade_level_2)),
            "enclave_mineral_trade_3_mut": dict(zip(trade_energy_for_minerals, trade_level_3)),
            "enclave_mineral_trade_3_rig": dict(zip(trade_energy_for_minerals, trade_level_3)),
            "enclave_mineral_trade_3_xur": dict(zip(trade_energy_for_minerals, trade_level_3)),
            "enclave_mineral_food_trade_1_mut": dict(zip(trade_food_for_minerals, trade_level_1)),
            "enclave_mineral_food_trade_1_rig": dict(zip(trade_food_for_minerals, trade_level_1)),
            "enclave_mineral_food_trade_1_xur": dict(zip(trade_food_for_minerals, trade_level_1)),
            "enclave_mineral_food_trade_2_mut": dict(zip(trade_food_for_minerals, trade_level_2)),
            "enclave_mineral_food_trade_2_rig": dict(zip(trade_food_for_minerals, trade_level_2)),
            "enclave_mineral_food_trade_2_xur": dict(zip(trade_food_for_minerals, trade_level_2)),
            "enclave_mineral_food_trade_3_mut": dict(zip(trade_food_for_minerals, trade_level_3)),
            "enclave_mineral_food_trade_3_rig": dict(zip(trade_food_for_minerals, trade_level_3)),
            "enclave_mineral_food_trade_3_xur": dict(zip(trade_food_for_minerals, trade_level_3)),
            "enclave_energy_trade_1_mut": dict(zip(trade_minerals_for_energy, trade_level_1)),
            "enclave_energy_trade_1_rig": dict(zip(trade_minerals_for_energy, trade_level_1)),
            "enclave_energy_trade_1_xur": dict(zip(trade_minerals_for_energy, trade_level_1)),
            "enclave_energy_trade_2_mut": dict(zip(trade_minerals_for_energy, trade_level_2)),
            "enclave_energy_trade_2_rig": dict(zip(trade_minerals_for_energy, trade_level_2)),
            "enclave_energy_trade_2_xur": dict(zip(trade_minerals_for_energy, trade_level_2)),
            "enclave_energy_trade_3_mut": dict(zip(trade_minerals_for_energy, trade_level_3)),
            "enclave_energy_trade_3_rig": dict(zip(trade_minerals_for_energy, trade_level_3)),
            "enclave_energy_trade_3_xur": dict(zip(trade_minerals_for_energy, trade_level_3)),
            "enclave_energy_food_trade_1_mut": dict(zip(trade_food_for_energy, trade_level_1)),
            "enclave_energy_food_trade_1_rig": dict(zip(trade_food_for_energy, trade_level_1)),
            "enclave_energy_food_trade_1_xur": dict(zip(trade_food_for_energy, trade_level_1)),
            "enclave_energy_food_trade_2_mut": dict(zip(trade_food_for_energy, trade_level_2)),
            "enclave_energy_food_trade_2_rig": dict(zip(trade_food_for_energy, trade_level_2)),
            "enclave_energy_food_trade_2_xur": dict(zip(trade_food_for_energy, trade_level_2)),
            "enclave_energy_food_trade_3_mut": dict(zip(trade_food_for_energy, trade_level_3)),
            "enclave_energy_food_trade_3_rig": dict(zip(trade_food_for_energy, trade_level_3)),
            "enclave_energy_food_trade_3_xur": dict(zip(trade_food_for_energy, trade_level_3)),
            "enclave_food_minerals_trade_1_mut": dict(zip(trade_minerals_for_food, trade_level_1)),
            "enclave_food_minerals_trade_1_rig": dict(zip(trade_minerals_for_food, trade_level_1)),
            "enclave_food_minerals_trade_1_xur": dict(zip(trade_minerals_for_food, trade_level_1)),
            "enclave_food_minerals_trade_2_mut": dict(zip(trade_minerals_for_food, trade_level_2)),
            "enclave_food_minerals_trade_2_rig": dict(zip(trade_minerals_for_food, trade_level_2)),
            "enclave_food_minerals_trade_2_xur": dict(zip(trade_minerals_for_food, trade_level_2)),
            "enclave_food_minerals_trade_3_mut": dict(zip(trade_minerals_for_food, trade_level_3)),
            "enclave_food_minerals_trade_3_rig": dict(zip(trade_minerals_for_food, trade_level_3)),
            "enclave_food_minerals_trade_3_xur": dict(zip(trade_minerals_for_food, trade_level_3)),
            "enclave_food_energy_trade_1_mut": dict(zip(trade_energy_for_food, trade_level_1)),
            "enclave_food_energy_trade_1_rig": dict(zip(trade_energy_for_food, trade_level_1)),
            "enclave_food_energy_trade_1_xur": dict(zip(trade_energy_for_food, trade_level_1)),
            "enclave_food_energy_trade_2_mut": dict(zip(trade_energy_for_food, trade_level_2)),
            "enclave_food_energy_trade_2_rig": dict(zip(trade_energy_for_food, trade_level_2)),
            "enclave_food_energy_trade_2_xur": dict(zip(trade_energy_for_food, trade_level_2)),
            "enclave_food_energy_trade_3_mut": dict(zip(trade_energy_for_food, trade_level_3)),
            "enclave_food_energy_trade_3_rig": dict(zip(trade_energy_for_food, trade_level_3)),
            "enclave_food_energy_trade_3_xur": dict(zip(trade_energy_for_food, trade_level_3)),
        }

    def _reset_state(self):
        logger.info(f"{self._logger_str} Resetting timeline state")
        self._systems_by_ingame_country_id = None
        self._planets_by_ingame_country_id = None
        self._country_by_ingame_planet_id = None
        self._species_by_ingame_id = None
        self._robot_species = None
        self._current_gamestate = None
        self._gamestate_dict = None
        self._player_country_id = None
        self._player_research_agreements = None
        self._player_sensor_links = None
        self._player_monthly_trade_info = None
        self._session = None
        self._logger_str = None
        self._date_in_days = None