# -*- coding: utf-8 -*-
"""YFPY module for making Yahoo Fantasy Sports REST API queries.

This module provides all available Yahoo Fantasy Sports API queries as callable methods on the YahooFantasySportsQuery
    class.

Attributes:
    logger (Logger): Module level logger for usage and debugging.

"""
__author__ = "Wren J. R. (uberfastman)"
__email__ = "uberfastman@uberfastman.dev"

import json
import logging
import time
import tempfile
import json
from pathlib import Path, PosixPath
from typing import Callable, Dict, List, Type, TypeVar, Union, Any

from requests import Response
from requests.exceptions import HTTPError
from yahoo_oauth import OAuth2

from yfpy.exceptions import YahooFantasySportsDataNotFound
from yfpy.logger import get_logger
from yfpy.models import YahooFantasyObject, DraftResult, Game, GameWeek, User, League, Standings, Settings, Player, \
    PositionType, StatCategories, Transaction, Scoreboard, Team, TeamPoints, TeamProjectedPoints, TeamStandings, \
    Roster, RosterPosition, Matchup
from yfpy.utils import jsonify_data, prettify_data, reformat_json_list, unpack_data

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)


# noinspection PyTypeChecker, PyUnresolvedReferences
class YahooFantasySportsQuery(object):
    """Yahoo Fantasy Sports REST API query CLASS to retrieve all types of fantasy sports data.
    """

    YFO = TypeVar("YFO", bound=YahooFantasyObject)

    def __init__(self, league_id: str, access_token: str, refresh_token: str, 
                 consumer_key: str, consumer_secret: str, game_id: int = None, game_code: str = "nfl",
                 offline: bool = False, all_output_as_json_str: bool = False, browser_callback: bool = True, 
                 retries: int = 3, backoff: int = 0):
        """Instantiate a YahooQueryObject for running queries against the Yahoo fantasy REST API.
        """
        self._yahoo_access_token = access_token
        self._yahoo_refresh_token = refresh_token
        self._yahoo_consumer_key = consumer_key
        self._yahoo_consumer_secret = consumer_secret
        self._browser_callback: bool = browser_callback
        self._retries: int = retries
        self._backoff: int = backoff

        self.fantasy_content_data_field: str = "fantasy_content"

        self.league_id: str = league_id
        self.game_id: int = game_id
        self.game_code: str = game_code

        self.offline: bool = offline
        self.all_output_as_json_str: bool = all_output_as_json_str

        self.league_key: str = None
        self.executed_queries: List[Dict[str, Any]] = []

        # Create a temporary directory for this session
        self._temp_dir = tempfile.TemporaryDirectory()
        self._auth_dir = Path(self._temp_dir.name)

        # Create token.json file in the temporary directory
        token_data = {
            "access_token": self._yahoo_access_token,
            "refresh_token": self._yahoo_refresh_token,
            "consumer_key": self._yahoo_consumer_key,
            "consumer_secret": self._yahoo_consumer_secret
        }
        with open(self._auth_dir / "token.json", "w") as token_file:
            json.dump(token_data, token_file)

        if not self.offline:
            self._authenticate()

    def _authenticate(self) -> None:
        """Authenticate with the Yahoo Fantasy Sports REST API.
    
        Returns:
            None
        """
        logger.debug("Authenticating with Yahoo.")
        
        # If consumer key and secret are not provided, try to load them from the private.json file
        if not self._yahoo_consumer_key or not self._yahoo_consumer_secret:
            private_json_path = self._auth_dir / "private.json"
            if private_json_path.is_file():
                with open(private_json_path) as yahoo_app_credentials:
                    auth_info = json.load(yahoo_app_credentials)
                    self._yahoo_consumer_key = auth_info["consumer_key"]
                    self._yahoo_consumer_secret = auth_info["consumer_secret"]
            else:
                logger.error("Consumer key and secret are not provided, and private.json does not exist.")
                return
    
       # Create OAuth2 object
        self.oauth = OAuth2(self._yahoo_consumer_key, self._yahoo_consumer_secret, 
                            from_file=str(self._auth_dir / "token.json"), 
                            browser_callback=self._browser_callback)
        
        if self._yahoo_access_token and self._yahoo_refresh_token:
            # Tokens are already provided, no need to authenticate again
            logger.debug("Tokens are already provided.")
            self.oauth.token = {
                'access_token': self._yahoo_access_token,
                'refresh_token': self._yahoo_refresh_token,
                'token_type': 'bearer',
                'expires_in': 3600,  # assuming a default value, you might want to adjust this
            }
            self.oauth.token_time = time.time()
            return
    
        # If tokens are not provided, complete OAuth2 3-legged handshake
        if not self.oauth.token_is_valid():
            logger.debug("Token is not valid or not provided, refreshing access token.")
            if not self._browser_callback:
                print("Visit the following URL, log in, and copy the code:")
                print(self.oauth.get_authorization_url())
                auth_code = input("Enter the code here: ")
                self.oauth.get_access_token(auth_code)
            else:
                self.oauth.refresh_access_token()
        logger.debug("Authentication successful, OAuth object assigned.")


    def cleanup(self) -> None:
        """Cleanup temporary files and directories."""
        # Close and remove the temporary directory
        self._temp_dir.cleanup()

    def get_response(self, url: str) -> Response:
        """Retrieve Yahoo Fantasy Sports data from the REST API.

        Args:
            url (str): REST API request URL string.

        Returns:
            Response: API response from Yahoo Fantasy Sports API request.

        """
        logger.debug(f"Making request to URL: {url}")
        response: Response = self.oauth.session.get(url, params={"format": "json"})

        status_code = response.status_code
        # when you exceed Yahoo's allowed data request limits, they throw a request status code of 999
        if status_code == 999:
            raise HTTPError("Yahoo data unavailable due to rate limiting. Please try again later.")

        if status_code == 401:
            self._authenticate()

        response_json = {}
        try:
            response_json = response.json()
            logger.debug(f"Response (JSON): {response_json}")
        except json.JSONDecodeError:
            response.raise_for_status()

        try:
            if (status_code // 100) != 2:
                # handle if the yahoo query returns an error
                if response_json.get("error"):
                    response_error_msg = response_json.get("error").get("description")
                    error_msg = f"Attempt to retrieve data at URL {response.url} failed with error: " \
                                f"\"{response_error_msg}\""
                    logger.error(error_msg)
                    raise YahooFantasySportsDataNotFound(error_msg, url=response.url)

            response.raise_for_status()

        except HTTPError as e:
            # retry with incremental back-off
            if self._retries > 0:
                self._retries -= 1
                self._backoff += 1
                logger.warning(f"Request for URL {url} failed with status code {response.status_code}. "
                               f"Retrying {self._retries} more time{'s' if self._retries > 1 else ''}...")
                time.sleep(0.3 * self._backoff)
                response = self.get_response(url)
            else:
                # log error and terminate query if status code is not 200 after 3 retries
                logger.error(f"Request failed with status code: {response.status_code} - {e}")
                response.raise_for_status()

        raw_response_data = response_json.get(self.fantasy_content_data_field)

        # extract data from "fantasy_content" field if it exists
        if raw_response_data:
            logger.debug(f"Data fetched with query URL: {response.url}")
            logger.debug(
                f"Response (Yahoo fantasy data extracted from: "
                f"\"{self.fantasy_content_data_field}\"): {raw_response_data}"
            )
        else:
            error_msg = f"No data found at URL {response.url} when attempting extraction from field: " \
                        f"\"{self.fantasy_content_data_field}\""
            logger.error(error_msg)
            raise YahooFantasySportsDataNotFound(error_msg, url=response.url)

        return response

    # noinspection GrazieInspection
    def query(self, url: str, data_key_list: Union[List[str], List[List[str]]], data_type_class: Type = None,
              sort_function: Callable = None) -> (Union[str, YFO, List[YFO], Dict[str, YFO]]):
        """Base query class to retrieve requested data from the Yahoo fantasy sports REST API.

        Args:
            url (str): REST API request URL string.
            data_key_list (list[str] | list[list[str]]): List of keys used to extract the specific data desired by the
                given query (supports strings and lists of strings). Supports lists containing only key strings such as
                ["game", "stat_categories"], and also supports lists containing key strings followed by lists of key
                strings such as ["team", ["team_points", "team_projected_points"]].
            data_type_class (:obj:`Type`, optional): Highest level data model type (if one exists for the retrieved
                data).
            sort_function (Callable of sort function, optional)): Optional lambda function to return sorted query
                results.

        Returns:
            object: Model class instance from yfpy/models.py, dictionary, or list (depending on query), with unpacked
            and parsed response data.

        """
        if not self.offline:
            response = self.get_response(url)
            raw_response_data = response.json().get(self.fantasy_content_data_field)

            # iterate through list of data keys and drill down to final desired data field
            for i in range(len(data_key_list)):
                if isinstance(raw_response_data, list):
                    if isinstance(data_key_list[i], list):
                        reformatted = reformat_json_list(raw_response_data)
                        raw_response_data = [
                            {data_key_list[i][0]: reformatted[data_key_list[i][0]]},
                            {data_key_list[i][1]: reformatted[data_key_list[i][1]]}
                        ]
                    else:
                        raw_response_data = reformat_json_list(raw_response_data)[data_key_list[i]]
                else:
                    if isinstance(data_key_list[i], list):
                        raw_response_data = [
                            {data_key_list[i][0]: raw_response_data[data_key_list[i][0]]},
                            {data_key_list[i][1]: raw_response_data[data_key_list[i][1]]}
                        ]
                    else:
                        raw_response_data = raw_response_data.get(data_key_list[i])

            if raw_response_data:
                logger.debug(f"Response (Yahoo fantasy data extracted from: {data_key_list}): {raw_response_data}")
            else:
                error_msg = f"No data found when attempting extraction from fields: {data_key_list}"
                logger.error(error_msg)
                raise YahooFantasySportsDataNotFound(error_msg, payload=data_key_list, url=response.url)

            # unpack, parse, and assign data types to all retrieved data content
            unpacked = unpack_data(raw_response_data, YahooFantasyObject)
            logger.debug(
                f"Unpacked and parsed JSON (Yahoo fantasy data wth parent type: {data_type_class}):\n{unpacked}")

            self.executed_queries.append({
                "url": response.url,
                "response_status_code": response.status_code,
                "response": response
            })

            # cast the highest level of data to type corresponding to query (if type exists)
            query_data = data_type_class(unpacked) if data_type_class else unpacked

            # sort data when applicable
            if sort_function and not isinstance(query_data, dict):
                query_data = sorted(query_data, key=sort_function)

            # flatten lists of single-key dicts of objects into lists of those objects
            if isinstance(query_data, list):
                last_data_key = data_key_list[-1]
                if last_data_key.endswith("s"):
                    query_data = [el[last_data_key[:-1]] for el in query_data]

            if self.all_output_as_json_str:
                return jsonify_data(query_data)
            else:
                return query_data

        else:
            logger.error("Cannot run Yahoo query while using offline mode! Please try again with offline=False.")

    def get_all_yahoo_fantasy_game_keys(self) -> List[Game]:
        """Retrieve all Yahoo Fantasy Sports game keys by ID (from year of inception to present), sorted by season/year.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_all_yahoo_fantasy_game_keys()
            [
              Game({
                "code": "nfl",
                "game_id": "50",
                "game_key": "50",
                "is_game_over": 1,
                "is_offseason": 1,
                "is_registration_over": 1,
                "name": "Football",
                "season": "1999",
                "type": "full",
                "url": "https://football.fantasysports.yahoo.com/archive/nfl/1999"
              }),
              ...,
              Game({...})
            ]

        Returns:
            list[Game]: List of YFPY Game instances.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/games;game_codes={self.game_code}",
            ["games"],
            sort_function=lambda x: x.get("game").season
        )

    # noinspection PyUnresolvedReferences
    def get_game_key_by_season(self, season: int) -> str:
        """Retrieve specific game key by season.

        Args:
            season (int): User defined season/year for which to retrieve the Yahoo Fantasy Sports game.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_game_key_by_season(2021)
            338

        Returns:
            str: The game key for a Yahoo Fantasy Sports game specified by season.

        """
        all_output_as_json = False
        if self.all_output_as_json_str:
            self.all_output_as_json_str = False
            all_output_as_json = True

        game_key = self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/games;game_codes={self.game_code};seasons={season}",
            ["games"]
        ).get("game").game_key

        if all_output_as_json:
            self.all_output_as_json_str = True

        return game_key

    def get_current_game_info(self) -> Game:
        """Retrieve game info for current fantasy season.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_current_game_info()
            Game({
              "code": "nfl",
              "game_id": "390",
              "game_key": "390",
              "game_weeks": [
                {
                  "game_week": {
                    "display_name": "1",
                    "end": "2019-09-09",
                    "start": "2019-09-05",
                    "week": "1"
                  }
                },
                ...
              ],
              "is_game_over": 0,
              "is_live_draft_lobby_active": 1,
              "is_offseason": 0,
              "is_registration_over": 0,
              "name": "Football",
              "position_types": [
                {
                  "position_type": {
                    "type": "O",
                    "display_name": "Offense"
                  }
                },
                ...
              ],
              "roster_positions": [
                {
                  "roster_position": {
                    "position": "QB",
                    "position_type": "O"
                  }
                },
                ...
              ],
              "season": "2019",
              "stat_categories": {
                "stats": [
                  {
                    "stat": {
                      "display_name": "GP",
                      "name": "Games Played",
                      "sort_order": "1",
                      "stat_id": 0
                    }
                  },
                  ...
              },
              "type": "full",
              "url": "https://football.fantasysports.yahoo.com/f1"
            })

        Returns:
            Game: YFPY Game instance.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/game/{self.game_code};"
            f"out=metadata,players,game_weeks,stat_categories,position_types,roster_positions",
            ["game"],
            Game
        )

    def get_current_game_metadata(self) -> Game:
        """Retrieve game metadata for current fantasy season.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_current_game_metadata()
            Game({
              "code": "nfl",
              "game_id": "390",
              "game_key": "390",
              "is_game_over": 0,
              "is_live_draft_lobby_active": 1,
              "is_offseason": 0,
              "is_registration_over": 0,
              "name": "Football",
              "season": "2019",
              "type": "full",
              "url": "https://football.fantasysports.yahoo.com/f1"
            })

        Returns:
            Game: YFPY Game instance.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/game/{self.game_code}/metadata",
            ["game"],
            Game
        )

    def get_game_info_by_game_id(self, game_id: int) -> Game:
        """Retrieve game info for specific game by ID.

        Args:
            game_id (int): Game ID of selected Yahoo Fantasy game corresponding to a specific year.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_game_info_by_game_id(390)
            Game({
              "code": "nfl",
              "game_id": "390",
              "game_key": "390",
              "game_weeks": [
                {
                  "game_week": {
                    "display_name": "1",
                    "end": "2019-09-09",
                    "start": "2019-09-05",
                    "week": "1"
                  }
                },
                ...
              ],
              "is_game_over": 0,
              "is_live_draft_lobby_active": 1,
              "is_offseason": 0,
              "is_registration_over": 0,
              "name": "Football",
              "position_types": [
                {
                  "position_type": {
                    "type": "O",
                    "display_name": "Offense"
                  }
                },
                ...
              ],
              "roster_positions": [
                {
                  "roster_position": {
                    "position": "QB",
                    "position_type": "O"
                  }
                },
                ...
              ],
              "season": "2019",
              "stat_categories": {
                "stats": [
                  {
                    "stat": {
                      "display_name": "GP",
                      "name": "Games Played",
                      "sort_order": "1",
                      "stat_id": 0
                    }
                  },
                  ...
              },
              "type": "full",
              "url": "https://football.fantasysports.yahoo.com/f1"
            })

        Returns:
            Game: YFPY Game instance.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/game/{game_id};"
            f"out=metadata,players,game_weeks,stat_categories,position_types,roster_positions",
            ["game"],
            Game
        )

    def get_game_metadata_by_game_id(self, game_id: int) -> Game:
        """Retrieve game metadata for specific game by ID.

        Args:
            game_id (int): Game ID of selected Yahoo Fantasy game corresponding to a specific year.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_game_metadata_by_game_id(331)
            Game({
              "code": "nfl",
              "game_id": "331",
              "game_key": "331",
              "is_game_over": 1,
              "is_offseason": 1,
              "is_registration_over": 1,
              "name": "Football",
              "season": "2014",
              "type": "full",
              "url": "https://football.fantasysports.yahoo.com/archive/nfl/2014"
            })

        Returns:
            Game: YFPY Game instance.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/game/{game_id}/metadata",
            ["game"],
            Game
        )

    def get_game_weeks_by_game_id(self, game_id: int) -> List[GameWeek]:
        """Retrieve all valid weeks of a specific game by ID.

        Args:
            game_id (int): Game ID of selected Yahoo Fantasy game corresponding to a specific year.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_game_weeks_by_game_id(331)
            [
              GameWeek({
                "display_name": "1",
                "end": "2014-09-08",
                "start": "2014-09-04",
                "week": "1"
              }),
              ...,
              GameWeek({
                "display_name": "17",
                "end": "2014-12-28",
                "start": "2014-12-23",
                "week": "17"
              })
            ]

        Returns:
            list[GameWeek]: List of YFPY GameWeek instances.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/game/{game_id}/game_weeks",
            ["game", "game_weeks"]
        )

    def get_game_stat_categories_by_game_id(self, game_id: int) -> StatCategories:
        """Retrieve all valid stat categories of a specific game by ID.

        Args:
            game_id (int): Game ID of selected Yahoo Fantasy game corresponding to a specific year.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_game_stat_categories_by_game_id(331)
            StatCategories({
              "stats": [
                {
                  "stat": {
                    "display_name": "GP",
                    "name": "Games Played",
                    "sort_order": "1",
                    "stat_id": 0
                  }
                },
                ...,
                {
                  "stat": {
                    "display_name": "Rush 1st Downs",
                    "name": "Rushing 1st Downs",
                    "sort_order": "1",
                    "stat_id": 81
                  }
                }
              ]
            })

        Returns:
            StatCategories: YFPY StatCategories instance.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/game/{game_id}/stat_categories",
            ["game", "stat_categories"],
            StatCategories
        )

    def get_game_position_types_by_game_id(self, game_id: int) -> List[PositionType]:
        """Retrieve all valid position types for specific game by ID sorted alphabetically by type.

        Args:
            game_id (int): Game ID of selected Yahoo Fantasy game corresponding to a specific year.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_game_position_types_by_game_id(331)
            [
              PositionType({
                "type": "O",
                "display_name": "Offense"
              }),
              ...,
              PositionType({
                "type": "K",
                "display_name": "Kickers"
              })
            ]

        Returns:
            list[PositionType]: List of YFPY PositionType instances.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/game/{game_id}/position_types",
            ["game", "position_types"],
            sort_function=lambda x: x.get("position_type").type
        )

    def get_game_roster_positions_by_game_id(self, game_id: int) -> List[RosterPosition]:
        """Retrieve all valid roster positions for specific game by ID sorted alphabetically by position.

        Args:
            game_id (int): Game ID of selected Yahoo Fantasy game corresponding to a specific year.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_game_roster_positions_by_game_id(331)
            [
              {RosterPosition({
                "position": "BN"
              }),
              ...,
              RosterPosition({
                "position": "WR",
                "position_type": "O"
              })
            ]

        Returns:
            list[RosterPosition]: List of YFPY RosterPosition instances.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/game/{game_id}/roster_positions",
            ["game", "roster_positions"],
            sort_function=lambda x: x.get("roster_position").position
        )

    def get_league_key(self, season: int = None) -> str:
        """Retrieve league key for selected league.

        Args:
            season (int): User defined season/year for which to retrieve the Yahoo Fantasy Sports league key.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_league_key(2021)
            331.l.729259

        Returns:
            str: League key string for selected league.

        """
        if not self.league_key:
            if season:
                return f"{self.get_game_key_by_season(season)}.l.{self.league_id}"
            elif self.game_id:
                return f"{self.get_game_metadata_by_game_id(self.game_id).game_key}.l.{self.league_id}"
            else:
                logger.warning(
                    "No game id or season/year provided, defaulting to current fantasy season.")
                return f"{self.get_current_game_metadata().game_key}.l.{self.league_id}"
        else:
            return self.league_key

    def get_current_user(self) -> User:
        """Retrieve metadata for current logged-in user.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_current_user()
            User({
              "guid": "USER_GUID_STRING"
            })

        Returns:
            User: YFPY User instance.

        """
        return self.query(
            "https://fantasysports.yahooapis.com/fantasy/v2/users;use_login=1/",
            ["users", "0", "user"],
            User
        )

    def get_user_games(self) -> List[Game]:
        """Retrieve game history for current logged-in user sorted by season/year.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_user_games()
            [
              Game({
                  "code": "nfl",
                  "game_id": "359",
                  "game_key": "359",
                  "is_game_over": 1,
                  "is_offseason": 1,
                  "is_registration_over": 1,
                  "name": "Football",
                  "season": "2016",
                  "type": "full",
                  "url": "https://football.fantasysports.yahoo.com/archive/nfl/2016"
              })
              ...,
              Game({...})
            ]

        Returns:
            list[Game]: List of YFPY Game instances.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/users;use_login=1/games;codes={self.game_code}/",
            ["users", "0", "user", "games"],
            sort_function=lambda x: x.get("game").season
        )

    def get_user_leagues_by_game_key(self, game_key: Union[int, str]) -> List[League]:
        """Retrieve league history for current logged-in user for specific game by game IDs/keys sorted by season/year.

        Args:
            game_key (int | str): The game_id (int) or game_key (str) for a specific Yahoo Fantasy game.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_user_leagues_by_game_key(331)
            [
              League({
                "allow_add_to_dl_extra_pos": 0,
                "current_week": "16",
                "draft_status": "postdraft",
                "edit_key": "16",
                "end_date": "2018-12-24",
                "end_week": "16",
                "game_code": "nfl",
                "iris_group_chat_id": "<group chat id>",
                "is_cash_league": "0",
                "is_finished": 1,
                "is_pro_league": "0",
                "league_id": "169896",
                "league_key": "380.l.169896",
                "league_type": "private",
                "league_update_timestamp": "1546498723",
                "logo_url": "<logo url>",
                "name": "League Name",
                "num_teams": 12,
                "password": null,
                "renew": "371_52364",
                "renewed": "390_78725",
                "scoring_type": "head",
                "season": "2018",
                "short_invitation_url": "<invite url>",
                "start_date": "2018-09-06",
                "start_week": "1",
                "url": "<league url>",
                "weekly_deadline": null
              }),
              ...,
              League({...})
            ]

        Returns:
            list[League]: List of YFPY League instances.

        """
        leagues = self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/users;use_login=1/games;game_keys={game_key}/leagues/",
            ["users", "0", "user", "games", "0", "game", "leagues"],
            sort_function=lambda x: x.get("league").season
        )
        return leagues if isinstance(leagues, list) else [leagues]

    def get_user_teams(self) -> List[Game]:
        """Retrieve teams for all leagues for current logged-in user for current game sorted by season/year.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_user_teams()
            [
              Game({
                "code": "nfl",
                "game_id": "359",
                "game_key": "359",
                "is_game_over": 1,
                "is_offseason": 1,
                "is_registration_over": 1,
                "name": "Football",
                "season": "2016",
                "teams": [
                  {
                    "team": {
                      "draft_grade": "A",
                      "draft_position": 9,
                      "draft_recap_url": "<draft recap url>",
                      "has_draft_grade": 1,
                      "league_scoring_type": "head",
                      "managers": [
                        {
                          "manager": {
                            "email": "<manager email>",
                            "guid": "<manager user guid>",
                            "image_url": "<manager user image url>",
                            "is_comanager": "1",
                            "manager_id": "14",
                            "nickname": "<manager nickname>"
                          }
                        }
                      ],
                      "name": "Legion",
                      "number_of_moves": "48",
                      "number_of_trades": "2",
                      "roster_adds": {
                        "coverage_type": "week",
                        "coverage_value": "17",
                        "value": "0"
                      },
                      "team_id": "1",
                      "team_key": "359.l.5521.t.1",
                      "team_logos": {
                        "team_logo": {
                          "size": "large",
                          "url": "<logo url>"
                        }
                      },
                      "url": "<team url>",
                      "waiver_priority": 11
                    }
                  }
                ],
                "type": "full",
                "url": "https://football.fantasysports.yahoo.com/archive/nfl/2016"
              })
              ...,
              Game({...})
          ]

        Returns:
            list[Game]: List of YFPY Game instances with "teams" attribute containing list of YFPY Team instances.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/users;use_login=1/games;codes={self.game_code}/teams/",
            ["users", "0", "user", "games"],
            sort_function=lambda x: x.get("game").season
        )

    def get_league_info(self) -> League:
        """Retrieve info for chosen league.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_league_info()
            League({
              "allow_add_to_dl_extra_pos": 0,
              "current_week": "16",
              "draft_status": "postdraft",
              "edit_key": "16",
              "end_date": "2014-12-22",
              "end_week": "16",
              "game_code": "nfl",
              "iris_group_chat_id": null,
              "is_cash_league": "0",
              "is_finished": 1,
              "is_pro_league": "1",
              "league_id": "729259",
              "league_key": "331.l.729259",
              "league_type": "public",
              "league_update_timestamp": "1420099793",
              "logo_url": "https://s.yimg.com/cv/api/default/20180206/default-league-logo@2x.png",
              "name": "Yahoo Public 729259",
              "num_teams": 10,
              "renew": null,
              "renewed": null,
              "scoreboard": {
                "week": "16",
                "matchups": [
                  ...
                ]
              },
              "scoring_type": "head",
              "season": "2014",
              "settings": {
                ...
              },
              "standings": {
                "teams": [
                    ...,
                    ...
                ],
                ...
              },
              "start_date": "2014-09-04",
              "start_week": "1",
              "url": "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259",
              "weekly_deadline": null
            })

        Returns:
            League: YFPY League instance.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()};"
            f"out=metadata,settings,standings,scoreboard,teams,players,draftresults,transactions",
            ["league"],
            League
        )

    def get_league_metadata(self) -> League:
        """Retrieve metadata for chosen league.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_league_metadata()
            League({
              "allow_add_to_dl_extra_pos": 0,
              "current_week": "16",
              "draft_status": "postdraft",
              "edit_key": "16",
              "end_date": "2014-12-22",
              "end_week": "16",
              "game_code": "nfl",
              "iris_group_chat_id": null,
              "is_cash_league": "0",
              "is_finished": 1,
              "is_pro_league": "1",
              "league_id": "729259",
              "league_key": "331.l.729259",
              "league_type": "public",
              "league_update_timestamp": "1420099793",
              "logo_url": "https://s.yimg.com/cv/api/default/20180206/default-league-logo@2x.png",
              "name": "Yahoo Public 729259",
              "num_teams": 10,
              "renew": null,
              "renewed": null,
              "scoring_type": "head",
              "season": "2014",
              "start_date": "2014-09-04",
              "start_week": "1",
              "url": "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259",
              "weekly_deadline": null
            })

        Returns:
            League: YFPY League instance.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/metadata",
            ["league"],
            League
        )

    def get_league_settings(self) -> Settings:
        """Retrieve settings (rules) for chosen league.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_league_settings()
            Settings({
              "cant_cut_list": "yahoo",
              "draft_time": "1408410000",
              "draft_type": "live",
              "has_multiweek_championship": 0,
              "has_playoff_consolation_games": true,
              "is_auction_draft": "0",
              "max_teams": "10",
              "num_playoff_consolation_teams": 4,
              "num_playoff_teams": "4",
              "pickem_enabled": "1",
              "player_pool": "ALL",
              "playoff_start_week": "15",
              "post_draft_players": "W",
              "roster_positions": [
                {
                  "roster_position": {
                    "count": 1,
                    "position": "QB",
                    "position_type": "O"
                  }
                },
                ...
              ],
              "scoring_type": "head",
              "stat_categories": {
                "stats": [
                  {
                    "stat": {
                      "display_name": "Pass Yds",
                      "enabled": "1",
                      "name": "Passing Yards",
                      "position_type": "O",
                      "sort_order": "1",
                      "stat_id": 4,
                      "stat_position_types": {
                        "stat_position_type": {
                          "position_type": "O"
                        }
                      }
                    }
                  },
                  ...
                ]
              },
              "stat_modifiers": {
                "stats": [
                  {
                    "stat": {
                      "stat_id": 4,
                      "value": "0.04"
                    }
                  },
                  ...
                ]
              },
              "trade_end_date": "2014-11-14",
              "trade_ratify_type": "yahoo",
              "trade_reject_time": "2",
              "uses_faab": "0",
              "uses_fractional_points": "1",
              "uses_lock_eliminated_teams": 1,
              "uses_negative_points": "1",
              "uses_playoff": "1",
              "uses_playoff_reseeding": 0,
              "waiver_rule": "gametime",
              "waiver_time": "2",
              "waiver_type": "R"
            })

        Returns:
            Settings: YFPY Settings instance.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/settings",
            ["league", "settings"],
            Settings
        )

    def get_league_standings(self) -> Standings:
        """Retrieve standings for chosen league.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_league_standings()
            Standings({
              "teams": [
                {
                  "team": {
                    "clinched_playoffs": 1,
                    "draft_grade": "C+",
                    "draft_position": 7,
                    "draft_recap_url":
                        "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259/8/draftrecap",
                    "has_draft_grade": 1,
                    "league_scoring_type": "head",
                    "managers": {
                      "manager": {
                        "guid": "PMTCFWSK5U5LI4SKWREUR56B5A",
                        "manager_id": "8",
                        "nickname": "--hidden--"
                      }
                    },
                    "name": "clam dam",
                    "number_of_moves": "27",
                    "number_of_trades": 0,
                    "roster_adds": {
                      "coverage_type": "week",
                      "coverage_value": "17",
                      "value": "0"
                    },
                    "team_id": "8",
                    "team_key": "331.l.729259.t.8",
                    "team_logos": {
                      "team_logo": {
                        "size": "large",
                        "url": "https://s.yimg.com/cv/apiv2/default/nfl/nfl_1.png"
                      }
                    },
                    "team_points": {
                      "coverage_type": "season",
                      "season": "2014",
                      "total": "1507.06"
                    },
                    "team_standings": {
                      "outcome_totals": {
                        "losses": 2,
                        "percentage": 0.857,
                        "ties": 0,
                        "wins": 12
                      },
                      "playoff_seed": "1",
                      "points_against": 1263.78,
                      "points_for": 1507.06,
                      "rank": 1,
                      "streak": {
                        "type": "win",
                        "value": "2"
                      }
                    },
                    "url": "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259/8",
                    "waiver_priority": 10
                  }
                },
                ...
              ]
            })

        Returns:
            Standings: YFPY Standings instance.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/standings",
            ["league", "standings"],
            Standings
        )

    def get_league_teams(self) -> List[Team]:
        """Retrieve teams for chosen league.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_league_teams()
            [
              Team({
                "clinched_playoffs": 1,
                "draft_grade": "B",
                "draft_position": 4,
                "draft_recap_url":
                  "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259/1/draftrecap",
                "has_draft_grade": 1,
                "league_scoring_type": "head",
                "managers": {
                  "manager": {
                    "guid": "BMACD7S5UXV7JIQX4PGGUVQJAU",
                    "manager_id": "1",
                    "nickname": "--hidden--"
                  }
                },
                "name": "Hellacious Hill 12",
                "number_of_moves": "71",
                "number_of_trades": 0,
                "roster_adds": {
                  "coverage_type": "week",
                  "coverage_value": "17",
                  "value": "0"
                },
                "team_id": "1",
                "team_key": "331.l.729259.t.1",
                "team_logos": {
                  "team_logo": {
                    "size": "large",
                    "url": "https://ct.yimg.com/cy/1441/24935131299_a8242dab70_192sq.jpg?ct=fantasy"
                  }
                },
                "url": "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259/1",
                "waiver_priority": 9
              }),
              ...,
              Team({...})
            ]

        Returns:
            list[Team]: List of YFPY Team instances.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/teams",
            ["league", "teams"]
        )

    def get_league_players(self, player_count_limit: int = None, player_count_start: int = 0,
                           is_retry: bool = False) -> List[Player]:
        """Retrieve valid players for chosen league.

        Args:
            player_count_limit (int): Maximum number of players to retreive.
            player_count_start (int): Index from which to retrieve all subsequent players.
            is_retry (bool): Boolean to indicate whether the method is being retried during error handling.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_league_players(50, 25)
            [
              Player({
                "bye_weeks": {
                  "week": "10"
                },
                "display_position": "K",
                "editorial_player_key": "nfl.p.3727",
                "editorial_team_abbr": "Ind",
                "editorial_team_full_name": "Indianapolis Colts",
                "editorial_team_key": "nfl.t.11",
                "eligible_positions": {
                  "position": "K"
                },
                "has_player_notes": 1,
                "headshot": {
                  "size": "small",
                  "url":
                    "https://s.yimg.com/iu/api/res/1.2/OpHvpCHjl_PQvkeQUgsjsA--~C
                    /YXBwaWQ9eXNwb3J0cztjaD0yMzM2O2NyPTE7Y3c9MTc5MDtkeD04NTc7ZHk9MDtmaT11bGNyb3A7aD02MDtxPTEwMDt
                    3PTQ2/https://s.yimg.com/xe/i/us/sp/v/nfl_cutout/players_l/08152019/3727.png"
                },
                "is_undroppable": "0",
                "name": {
                  "ascii_first": "Adam",
                  "ascii_last": "Vinatieri",
                  "first": "Adam",
                  "full": "Adam Vinatieri",
                  "last": "Vinatieri"
                },
                "player_id": "3727",
                "player_key": "331.p.3727",
                "player_notes_last_timestamp": 1568758320,
                "position_type": "K",
                "primary_position": "K",
                "uniform_number": "4"
              }),
              ...,
              Player({...})
            ]

        Returns:
            list[Player]: List of YFPY Player instances.

        """
        league_player_count = player_count_start
        all_players_retrieved = False
        league_player_data = []
        league_player_retrieval_limit = 25
        while not all_players_retrieved:

            try:
                league_player_query_data = self.query(
                    f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/players;"
                    f"start={league_player_count};count={league_player_retrieval_limit if not is_retry else 1}",
                    ["league", "players"]
                )

                league_players = (league_player_query_data if isinstance(league_player_query_data, list) else
                                  [league_player_query_data])
                league_player_count_from_query = len(league_players)

                if player_count_limit:
                    if (league_player_count + league_player_count_from_query) < player_count_limit:
                        league_player_count += league_player_count_from_query
                        league_player_data.extend(league_players)

                    else:
                        for ndx in range(player_count_limit - league_player_count):
                            league_player_data.append(league_players[ndx])
                        league_player_count += (player_count_limit - league_player_count)
                        all_players_retrieved = True

                else:
                    league_player_count += league_player_count_from_query
                    league_player_data.extend(league_players)

            except YahooFantasySportsDataNotFound as yfpy_err:
                if not is_retry:
                    payload = yfpy_err.payload
                    if payload:
                        logger.debug("No more league player data available.")
                        all_players_retrieved = True
                    else:
                        logger.warning(
                            f"Error retrieving player batch: "
                            f"{league_player_count}-{league_player_count + league_player_retrieval_limit - 1}. "
                            f"Attempting to retrieve individual players from batch.")

                        player_retrieval_successes = []
                        player_retrieval_failures = []
                        for i in range(25):
                            try:
                                player_data = self.get_league_players(
                                    player_count_limit=league_player_count + 1,
                                    player_count_start=league_player_count,
                                    is_retry=True
                                )
                                player_retrieval_successes.extend(player_data)

                            except YahooFantasySportsDataNotFound as nested_yfpy_err:
                                player_retrieval_failures.append(
                                    {
                                        "failed_player_retrieval_index": league_player_count,
                                        "failed_player_retrieval_url": nested_yfpy_err.url,
                                        "failed_player_retrieval_message": nested_yfpy_err.message
                                    }
                                )

                            league_player_count += 1

                        league_player_data.extend(player_retrieval_successes)
                        logger.warning(f"Players retrieval failures:\n{prettify_data(player_retrieval_failures)}")

                else:
                    raise yfpy_err

            logger.debug(f"League player count: {league_player_count}")

        return league_player_data

    def get_league_draft_results(self) -> List[DraftResult]:
        """Retrieve draft results for chosen league.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_league_draft_results()
            [
              DraftResult({
                "pick": 1,
                "round": 1,
                "team_key": "331.l.729259.t.4",
                "player_key": "331.p.9317"
              }),
              ...,
              DraftResult({...})
            ]

        Returns:
            list[DraftResult]: List of YFPY DraftResult instances.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/draftresults",
            ["league", "draft_results"]
        )

    def get_league_transactions(self) -> List[Transaction]:
        """Retrieve transactions for chosen league.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_league_transactions()
            [
              Transaction({
                "players": [
                  {
                    "player": {
                      "display_position": "RB",
                      "editorial_team_abbr": "NO",
                      "name": {
                        "ascii_first": "Kerwynn",
                        "ascii_last": "Williams",
                        "first": "Kerwynn",
                        "full": "Kerwynn Williams",
                        "last": "Williams"
                      },
                      "player_id": "26853",
                      "player_key": "331.p.26853",
                      "position_type": "O",
                      "transaction_data": {
                        "destination_team_key": "331.l.729259.t.1",
                        "destination_team_name": "Hellacious Hill 12",
                        "destination_type": "team",
                        "source_type": "freeagents",
                        "type": "add"
                      }
                    }
                  }
                ],
                "status": "successful",
                "timestamp": "1419188151",
                "transaction_id": "282",
                "transaction_key": "331.l.729259.tr.282",
                "type": "add/drop"
              }),
              ...,
              Transaction({...})
            ]

        Returns:
            list[Transaction]: List of YFPY Transaction instances.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/transactions",
            ["league", "transactions"]
        )

    def get_league_scoreboard_by_week(self, chosen_week: int) -> Scoreboard:
        """Retrieve scoreboard for chosen league by week.

        Args:
            chosen_week (int): Selected week for which to retrieve data.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_league_scoreboard_by_week(1)
            Scoreboard({
              "week": "1",
              "matchups": [
                {
                  "matchup": {
                    "is_consolation": "0",
                    "is_matchup_recap_available": 1,
                    "is_playoffs": "0",
                    "is_tied": 0,
                    "matchup_grades": [
                      {
                        "matchup_grade": {
                          "grade": "B",
                          "team_key": "331.l.729259.t.1"
                        }
                      },
                      {
                        "matchup_grade": {
                          "grade": "B",
                          "team_key": "331.l.729259.t.2"
                        }
                      }
                    ],
                    "matchup_recap_title": "Wax On Wax Off Gets Victory Against Hellacious Hill 12",
                    "matchup_recap_url":
                        "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259/recap?
                        week=1&mid1=1&mid2=2",
                    "status": "postevent",
                    "teams": [
                      {
                        "team": {
                            <team data>
                        }
                      },
                      {
                        "team": {
                            <team data>
                        }
                      }
                    ],
                    "week": "1",
                    "week_end": "2014-09-08",
                    "week_start": "2014-09-04",
                    "winner_team_key": "331.l.729259.t.2"
                  }
                },
                ...
              ]
            })

        Returns:
            Scoreboard: YFPY Scoreboard instance.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/scoreboard;"
            f"week={chosen_week}",
            ["league", "scoreboard"],
            Scoreboard
        )

    def get_league_matchups_by_week(self, chosen_week: int) -> List[Matchup]:
        """Retrieve matchups for chosen league by week.

        Args:
            chosen_week (int): Selected week for which to retrieve data.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_league_matchups_by_week(1)
            [
              Matchup({
                "is_consolation": "0",
                "is_matchup_recap_available": 1,
                "is_playoffs": "0",
                "is_tied": 0,
                "matchup_grades": [
                  {
                    "matchup_grade": {
                      "grade": "B",
                      "team_key": "331.l.729259.t.1"
                    }
                  },
                  {
                    "matchup_grade": {
                      "grade": "B",
                      "team_key": "331.l.729259.t.2"
                    }
                  }
                ],
                "matchup_recap_title": "Wax On Wax Off Gets Victory Against Hellacious Hill 12",
                "matchup_recap_url":
                  "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259/recap?
                  week=1&mid1=1&mid2=2",
                "status": "postevent",
                "teams": [
                  {
                    "team": {
                      <team data>
                    }
                  },
                  {
                    "team": {
                      <team data>
                    }
                  }
                ],
                "week": "1",
                "week_end": "2014-09-08",
                "week_start": "2014-09-04",
                "winner_team_key": "331.l.729259.t.2"
              }),
              ...,
              Matchup({...})
            ]

        Returns:
            list[Matchup]: List of YFPY Matchup instances.

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/scoreboard;"
            f"week={chosen_week}",
            ["league", "scoreboard", "0", "matchups"]
        )

    def get_team_info(self, team_id: Union[str, int]) -> Team:
        """Retrieve info of specific team by team_id for chosen league.

        Args:
            team_id (str | int): Selected team ID for which to retrieva data (can be integers 1 through n where n is the
                number of teams in the league).

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_team_info(1)
            Team({
              "clinched_playoffs": 1,
              "draft_grade": "B",
              "draft_position": 4,
              "draft_recap_url": "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259/1/draftrecap",
              "draft_results": [
                ...
              ],
              "has_draft_grade": 1,
              "league_scoring_type": "head",
              "managers": {
                "manager": {
                  "guid": "BMACD7S5UXV7JIQX4PGGUVQJAU",
                  "manager_id": "1",
                  "nickname": "--hidden--"
                }
              },
              "matchups": [
                ...
              ],
              "name": "Hellacious Hill 12",
              "number_of_moves": "71",
              "number_of_trades": 0,
              "roster": {
                ...
              },
              "roster_adds": {
                "coverage_type": "week",
                "coverage_value": "17",
                "value": "0"
              },
              "team_id": "1",
              "team_key": "331.l.729259.t.1",
              "team_logos": {
                "team_logo": {
                  "size": "large",
                  "url": "https://ct.yimg.com/cy/1441/24935131299_a8242dab70_192sq.jpg?ct=fantasy"
                }
              },
              "team_points": {
                "coverage_type": "season",
                "season": "2014",
                "total": "1409.24"
              },
              "team_standings": {
                "outcome_totals": {
                  "losses": 5,
                  "percentage": 0.643,
                  "ties": 0,
                  "wins": 9
                },
                "playoff_seed": "2",
                "points_against": 1266.6599999999999,
                "points_for": 1409.24,
                "rank": 2,
                "streak": {
                  "type": "win",
                  "value": "1"
                }
              },
              "url": "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259/1",
              "waiver_priority": 9
            })

        Returns:
            Team: YFPY Team instance.

        """
        team_key = f"{self.get_league_key()}.t.{team_id}"
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/team/{team_key};"
            f"out=metadata,stats,standings,roster,draftresults,matchups",
            ["team"],
            Team
        )

    def get_team_metadata(self, team_id: Union[str, int]) -> Team:
        """Retrieve metadata of specific team by team_id for chosen league.

        Args:
            team_id (str | int): Selected team ID for which to retrieva data (can be integers 1 through n where n is the
                number of teams in the league).

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_team_metadata(1)
            Team({
              "team_key": "331.l.729259.t.1",
              "team_id": "1",
              "name": "Hellacious Hill 12",
              "url": "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259/1",
              "team_logos": {
                "team_logo": {
                  "size": "large",
                  "url": "https://ct.yimg.com/cy/1441/24935131299_a8242dab70_192sq.jpg?ct=fantasy"
                }
              },
              "waiver_priority": 9,
              "number_of_moves": "71",
              "number_of_trades": 0,
              "roster_adds": {
                "coverage_type": "week",
                "coverage_value": "17",
                "value": "0"
              },
              "clinched_playoffs": 1,
              "league_scoring_type": "head",
              "draft_position": 4,
              "has_draft_grade": 1,
              "draft_grade": "B",
              "draft_recap_url": "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259/1/draftrecap",
              "managers": {
                "manager": {
                  "guid": "BMACD7S5UXV7JIQX4PGGUVQJAU",
                  "manager_id": "1",
                  "nickname": "--hidden--"
                }
              }
            })

        Returns:
            Team: YFPY Team instance.

        """
        team_key = f"{self.get_league_key()}.t.{team_id}"
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/team/{team_key}/metadata",
            ["team"],
            Team
        )

    def get_team_stats(self, team_id: Union[str, int]) -> TeamPoints:
        """Retrieve stats of specific team by team_id for chosen league.

        Args:
            team_id (str | int): Selected team ID for which to retrieva data (can be integers 1 through n where n is the
                number of teams in the league).

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_team_stats(1)
            TeamPoints({
              "coverage_type": "season",
              "season": "2014",
              "total": "1409.24"
            })

        Returns:
            TeamPoints: YFPY TeamPoints instance.

        """
        team_key = f"{self.get_league_key()}.t.{team_id}"
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/team/{team_key}/stats",
            ["team", "team_points"],
            TeamPoints
        )

    def get_team_stats_by_week(
            self, team_id: Union[str, int], chosen_week: Union[int, str] = "current"
    ) -> Dict[str, Union[TeamPoints, TeamProjectedPoints]]:
        """Retrieve stats of specific team by team_id and by week for chosen league.

        Args:
            team_id (str | int): Selected team ID for which to retrieva data (can be integers 1 through n where n is the
                number of teams in the league).
            chosen_week (int): Selected week for which to retrieve data.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_team_stats_by_week(1, 1)
            {
              "team_points": TeamPoints({
                "coverage_type": "week",
                "total": "95.06",
                "week": "1"
              }),
              "team_projected_points": TeamProjectedPoints({
                "coverage_type": "week",
                "total": "78.85",
                "week": "1"
              })
            }

        Returns:
            dict[str, TeamPoints | TeamProjectedPoints]: Dictionary containing keys "team_points" and
                "team_projected_points" with respective values YFPY TeamPoints and YFPY TeamProjectedPoints instances.

        """
        team_key = f"{self.get_league_key()}.t.{team_id}"
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/team/{team_key}/stats;type=week;week={chosen_week}",
            ["team", ["team_points", "team_projected_points"]]
        )

    def get_team_standings(self, team_id: Union[str, int]) -> TeamStandings:
        """Retrieve standings of specific team by team_id for chosen league.

        Args:
            team_id (str | int): Selected team ID for which to retrieva data (can be integers 1 through n where n is the
                number of teams in the league).

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_team_standings(1)
            TeamStandings({
              "rank": 2,
              "playoff_seed": "2",
              "outcome_totals": {
                "losses": 5,
                "percentage": 0.643,
                "ties": 0,
                "wins": 9
              },
              "streak": {
                "type": "win",
                "value": "1"
              },
              "points_for": "1409.24",
              "points_against": 1266.6599999999999
            })

        Returns:
            TeamStandings: YFPY TeamStandings instance.

        """
        team_key = f"{self.get_league_key()}.t.{team_id}"
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/team/{team_key}/standings",
            ["team", "team_standings"],
            TeamStandings
        )

    def get_team_roster_by_week(self, team_id: Union[str, int], chosen_week: Union[int, str] = "current") -> Roster:
        """Retrieve roster of specific team by team_id and by week for chosen league.

        Args:
            team_id (str | int): Selected team ID for which to retrieva data (can be integers 1 through n where n is the
                number of teams in the league).
            chosen_week (int): Selected week for which to retrieve data.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_team_roster_by_week(1, 1)
            Roster({
              "coverage_type": "week",
              "week": "1",
              "is_editable": 0,
              "players": [
                {
                  "player": {
                    "bye_weeks": {
                      "week": "10"
                    },
                    "display_position": "QB",
                    "editorial_player_key": "nfl.p.5228",
                    "editorial_team_abbr": "NE",
                    "editorial_team_full_name": "New England Patriots",
                    "editorial_team_key": "nfl.t.17",
                    "eligible_positions": {
                      "position": "QB"
                    },
                    "has_player_notes": 1,
                    "headshot": {
                      "size": "small",
                      "url": "https://s.yimg.com/iu/api/res/1.2/_U9UJlrYMsJ22DpA..S3zg--~C
                        /YXBwaWQ9eXNwb3J0cztjaD0yMzM2O2NyPTE7Y3c9MTc5MDtkeD04NTc7ZHk9MDtmaT11bGNyb3A7aD02MDtxPTEwMDt
                        3PTQ2/https://s.yimg.com/xe/i/us/sp/v/nfl_cutout/players_l/08212019/5228.png"
                    },
                    "is_undroppable": "0",
                    "name": {
                      "ascii_first": "Tom",
                      "ascii_last": "Brady",
                      "first": "Tom",
                      "full": "Tom Brady",
                      "last": "Brady"
                    },
                    "player_id": "5228",
                    "player_key": "331.p.5228",
                    "player_notes_last_timestamp": 1568837880,
                    "position_type": "O",
                    "primary_position": "QB",
                    "selected_position": {
                      "coverage_type": "week",
                      "is_flex": 0,
                      "position": "QB",
                      "week": "1"
                    },
                    "uniform_number": "12"
                  }
                },
                ...
              ]
            })

        Returns:
            Roster: YFPY Roster instance.

        """
        team_key = f"{self.get_league_key()}.t.{team_id}"
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/team/{team_key}/roster;week={chosen_week}",
            ["team", "roster"],
            Roster
        )

    def get_team_roster_player_info_by_week(self, team_id: Union[str, int],
                                            chosen_week: Union[int, str] = "current") -> List[Player]:
        """Retrieve roster with ALL player info of specific team by team_id and by week for chosen league.

        Args:
            team_id (str | int): Selected team ID for which to retrieva data (can be integers 1 through n where n is the
                number of teams in the league).
            chosen_week (int): Selected week for which to retrieve data.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_team_roster_player_info_by_week(1, 1)
            [
              Player({
                "bye_weeks": {
                  "week": "10"
                },
                "display_position": "QB",
                "draft_analysis": {
                  "average_pick": "65.9",
                  "average_round": "7.6",
                  "average_cost": "5.0",
                  "percent_drafted": "1.00"
                },
                "editorial_player_key": "nfl.p.5228",
                "editorial_team_abbr": "NE",
                "editorial_team_full_name": "New England Patriots",
                "editorial_team_key": "nfl.t.17",
                "eligible_positions": {
                  "position": "QB"
                },
                "has_player_notes": 1,
                "headshot": {
                  "size": "small",
                  "url": "https://s.yimg.com/iu/api/res/1.2/_U9UJlrYMsJ22DpA..S3zg--~C
                    /YXBwaWQ9eXNwb3J0cztjaD0yMzM2O2NyPTE7Y3c9MTc5MDtkeD04NTc7ZHk9MDtmaT11bGNyb3A7aD02MDtxPTEwMDt3PTQ2/
                    https://s.yimg.com/xe/i/us/sp/v/nfl_cutout/players_l/08212019/5228.png"
                },
                "is_undroppable": "0",
                "name": {
                  "ascii_first": "Tom",
                  "ascii_last": "Brady",
                  "first": "Tom",
                  "full": "Tom Brady",
                  "last": "Brady"
                },
                "ownership": {
                  "ownership_type": "team",
                  "owner_team_key": "331.l.729259.t.1",
                  "owner_team_name": "Hellacious Hill 12"
                },
                "percent_owned": {
                  "coverage_type": "week",
                  "week": "17",
                  "value": 99,
                  "delta": "0"
                },
                "player_id": "5228",
                "player_key": "331.p.5228",
                "player_notes_last_timestamp": 1568837880,
                "player_points": {
                  "coverage_type": "week",
                  "week": "1",
                  "total": 10.26
                },
                "player_stats": {
                  "coverage_type": "week",
                  "week": "1",
                  "stats": [
                    {
                      "stat": {
                        "stat_id": "4",
                        "value": "249"
                      }
                    },
                    ...
                  ]
                },
                "position_type": "O",
                "primary_position": "QB",
                "selected_position": {
                  "coverage_type": "week",
                  "is_flex": 0,
                  "position": "QB",
                  "week": "1"
                },
                "uniform_number": "12"
              }),
              ...,
              Player({...})
            ]

        Returns:
            list[Player]: List of YFPY Player instances containing attributes "draft_analysis", "ownership",
                "percent_owned", and "player_stats".

        """
        team_key = f"{self.get_league_key()}.t.{team_id}"
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/team/{team_key}/roster;week={chosen_week}/players;"
            f"out=metadata,stats,ownership,percent_owned,draft_analysis",
            ["team", "roster", "0", "players"]
        )

    def get_team_roster_player_info_by_date(self, team_id: Union[str, int],
                                            chosen_date: str = None) -> List[Player]:
        """Retrieve roster with ALL player info of specific team by team_id and by date for chosen league.

        Note:
            This applies to MLB, NBA, and NHL leagues, but does NOT apply to NFL leagues.
            This query will FAIL if you pass it an INVALID date string!

        Args:
            team_id (str | int): Selected team ID for which to retrieva data (can be integers 1 through n where n is the
                number of teams in the league).
            chosen_date (str): Selected date for which to retrieve data. REQUIRED FORMAT: YYYY-MM-DD (Ex. 2011-05-01)

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_team_roster_player_info_by_date(1, "2011-05-01")
            [
              Player({
                "display_position": "C",
                "draft_analysis": {
                  "average_pick": 33.2,
                  "average_round": 3.5,
                  "average_cost": 39.2,
                  "percent_drafted": 1.0
                },
                "editorial_player_key": "nhl.p.3981",
                "editorial_team_abbr": "Chi",
                "editorial_team_full_name": "Chicago Blackhawks",
                "editorial_team_key": "nhl.t.4",
                "eligible_positions": [
                  "C",
                  "F"
                ],
                "has_player_notes": 1,
                "headshot": {
                  "size": "small",
                  "url": "https://s.yimg.com/iu/api/res/1.2/tz.KOMoEiBDch6AJAGaUtg--~C/
                    YXBwaWQ9eXNwb3J0cztjaD0yMzM2O2NyPTE7Y3c9MTc5MDtkeD04NTc7ZHk9MDtmaT11bGNyb3A7aD02MDtxPTEwMDt3PTQ2/
                    https://s.yimg.com/xe/i/us/sp/v/nhl_cutout/players_l/11032021/3981.png"
                },
                "is_editable": 0,
                "is_undroppable": 0,
                "name": {
                  "ascii_first": "Jonathan",
                  "ascii_last": "Toews",
                  "first": "Jonathan",
                  "full": "Jonathan Toews",
                  "last": "Toews"
                },
                "ownership": {
                  "ownership_type": "team",
                  "owner_team_key": "303.l.69624.t.2",
                  "owner_team_name": "The Bateleurs"
                },
                "percent_owned": {
                  "coverage_type": "week",
                  "week": 25,
                  "value": 98,
                  "delta": -1.0
                },
                "player_id": 3981,
                "player_key": "303.p.3981",
                "player_notes_last_timestamp": 1651606838,
                "player_stats": {
                  "coverage_type": "date",
                  "stats": [
                    {
                      "stat": {
                        "stat_id": 1,
                        "value": 1.0
                      }
                    },
                    ...
                  ]
                },
                "position_type": "P",
                "primary_position": "C",
                "selected_position": {
                  "coverage_type": "date",
                  "is_flex": 0,
                  "position": "C"
                },
                "uniform_number": 19
              }),
              ...,
              Player({...})
            ]

        Returns:
            list[Player]: List of YFPY Player instances containing attributes "draft_analysis", "ownership",
                "percent_owned", and "player_stats".

        """
        team_key = f"{self.get_league_key()}.t.{team_id}"
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/team/{team_key}/"
            f"roster{';date=' + str(chosen_date) if chosen_date else ''}/players;"
            f"out=metadata,stats,ownership,percent_owned,draft_analysis",
            ["team", "roster", "0", "players"]
        )

    def get_team_roster_player_stats(self, team_id: Union[str, int]) -> List[Player]:
        """Retrieve roster with ALL player info for the season of specific team by team_id and for chosen league.

        Args:
            team_id (str | int): Selected team ID for which to retrieva data (can be integers 1 through n where n is the
                number of teams in the league).

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_team_roster_player_stats(1)
            [
              Player({
                "bye_weeks": {
                  "week": "10"
                },
                "display_position": "QB",
                "draft_analysis": {
                  "average_pick": "65.9",
                  "average_round": "7.6",
                  "average_cost": "5.0",
                  "percent_drafted": "1.00"
                },
                "editorial_player_key": "nfl.p.5228",
                "editorial_team_abbr": "NE",
                "editorial_team_full_name": "New England Patriots",
                "editorial_team_key": "nfl.t.17",
                "eligible_positions": {
                  "position": "QB"
                },
                "has_player_notes": 1,
                "headshot": {
                  "size": "small",
                  "url": "https://s.yimg.com/iu/api/res/1.2/_U9UJlrYMsJ22DpA..S3zg--~C
                    /YXBwaWQ9eXNwb3J0cztjaD0yMzM2O2NyPTE7Y3c9MTc5MDtkeD04NTc7ZHk9MDtmaT11bGNyb3A7aD02MDtxPTEwMDt3PTQ
                    2/https://s.yimg.com/xe/i/us/sp/v/nfl_cutout/players_l/08212019/5228.png"
                },
                "is_undroppable": "0",
                "name": {
                  "ascii_first": "Tom",
                  "ascii_last": "Brady",
                  "first": "Tom",
                  "full": "Tom Brady",
                  "last": "Brady"
                },
                "player_id": "5228",
                "player_key": "331.p.5228",
                "player_notes_last_timestamp": 1568837880,
                "player_points": {
                  "coverage_type": "season",
                  "total": 287.06
                },
                "player_stats": {
                  "coverage_type": "season",
                  "stats": [
                    {
                      "stat": {
                        "stat_id": "4",
                        "value": "4109"
                      }
                    },
                    ...
                  ]
                },
                "position_type": "O",
                "primary_position": "QB",
                "selected_position": {
                  "coverage_type": "week",
                  "is_flex": 0,
                  "position": "QB",
                  "week": "16"
                },
                "uniform_number": "12"
              }),
              ...,
              Player({...})
            ]

        Returns:
            list[Player]: List of YFPY Player instances containing attributes "draft_analysis", "ownership",
                "percent_owned", and "player_stats".

        """
        team_key = f"{self.get_league_key()}.t.{team_id}"
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/team/{team_key}/roster/players/stats;type=season",
            ["team", "roster", "0", "players"]
        )

    def get_team_roster_player_stats_by_week(self, team_id: Union[str, int],
                                             chosen_week: Union[int, str] = "current") -> List[Player]:
        """Retrieve roster with player stats of specific team by team_id and by week for chosen league.

        Args:
            team_id (str | int): Selected team ID for which to retrieva data (can be integers 1 through n where n is the
                number of teams in the league).
            chosen_week (int): Selected week for which to retrieve data.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_team_roster_player_stats_by_week(1, 1)
            [
              Player({
                "bye_weeks": {
                  "week": "10"
                },
                "display_position": "QB",
                "editorial_player_key": "nfl.p.5228",
                "editorial_team_abbr": "NE",
                "editorial_team_full_name": "New England Patriots",
                "editorial_team_key": "nfl.t.17",
                "eligible_positions": {
                  "position": "QB"
                },
                "has_player_notes": 1,
                "headshot": {
                  "size": "small",
                  "url": "https://s.yimg.com/iu/api/res/1.2/_U9UJlrYMsJ22DpA..S3zg--~C
                    /YXBwaWQ9eXNwb3J0cztjaD0yMzM2O2NyPTE7Y3c9MTc5MDtkeD04NTc7ZHk9MDtmaT11bGNyb3A7aD02MDtxPTEwMDt
                    3PTQ2/https://s.yimg.com/xe/i/us/sp/v/nfl_cutout/players_l/08212019/5228.png"
                },
                "is_undroppable": "0",
                "name": {
                  "ascii_first": "Tom",
                  "ascii_last": "Brady",
                  "first": "Tom",
                  "full": "Tom Brady",
                  "last": "Brady"
                },
                "player_id": "5228",
                "player_key": "331.p.5228",
                "player_notes_last_timestamp": 1568837880,
                "player_points": {
                  "coverage_type": "week",
                  "week": "1",
                  "total": 10.26
                },
                "player_stats": {
                  "coverage_type": "week",
                  "week": "1",
                  "stats": [
                    {
                      "stat": {
                        "stat_id": "4",
                        "value": "249"
                      }
                    },
                    ...
                  ]
                },
                "position_type": "O",
                "primary_position": "QB",
                "selected_position": {
                  "coverage_type": "week",
                  "is_flex": 0,
                  "position": "QB",
                  "week": "1"
                },
                "uniform_number": "12"
              }),
              ...,
              Player({...})
            ]

        Returns:
            list[Player]: List of YFPY Player instances containing attribute "player_stats".

        """
        team_key = f"{self.get_league_key()}.t.{team_id}"
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/team/{team_key}/roster;week={chosen_week}/players/stats",
            ["team", "roster", "0", "players"]
        )

    def get_team_draft_results(self, team_id: Union[str, int]) -> List[DraftResult]:
        """Retrieve draft results of specific team by team_id for chosen league.

        Args:
            team_id (str | int): Selected team ID for which to retrieva data (can be integers 1 through n where n is the
                number of teams in the league).

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_team_draft_results(1)
            [
              DraftResult({
                "pick": 4,
                "round": 1,
                "team_key": "331.l.729259.t.1",
                "player_key": "331.p.8256"
              }),
              ...,
              DraftResults({...})
            ]

        Returns:
            list[DraftResult]: List of YFPY DraftResult instances.

        """
        team_key = f"{self.get_league_key()}.t.{team_id}"
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/team/{team_key}/draftresults",
            ["team", "draft_results"]
        )

    def get_team_matchups(self, team_id: Union[str, int]) -> List[Matchup]:
        """Retrieve matchups of specific team by team_id for chosen league.

        Args:
            team_id (str | int): Selected team ID for which to retrieva data (can be integers 1 through n where n is the
                number of teams in the league).

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_team_matchups(1)
            [
                Matchup({
                  <matchup data> (see get_league_matchups_by_week docstring for matchup data example)
                })
            ]

        Returns:
            list[Matchup]: List of YFPY Matchup instances.

        """
        team_key = f"{self.get_league_key()}.t.{team_id}"
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/team/{team_key}/matchups",
            ["team", "matchups"]
        )

    def get_player_stats_for_season(self, player_key: str, limit_to_league_stats: bool = True) -> Player:
        """Retrieve stats of specific player by player_key for the entire season for chosen league.

        Args:
            player_key (str): The player key of chosen player (example: 331.p.7200 - <game_id>.p.<player_id>).
            limit_to_league_stats (bool): Boolean (default: True) to limit the retrieved player stats to those for the
                selected league. When set to False, query retrieves all player stats for the game (NFL, NHL, NBA, MLB).

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_player_stats_for_season("331.p.7200")
            Player({
              "bye_weeks": {
                "week": "9"
              },
              "display_position": "QB",
              "editorial_player_key": "nfl.p.7200",
              "editorial_team_abbr": "GB",
              "editorial_team_full_name": "Green Bay Packers",
              "editorial_team_key": "nfl.t.9",
              "eligible_positions": {
                "position": "QB"
              },
              "has_player_notes": 1,
              "headshot": {
                "size": "small",
                "url": "https://s.yimg.com/iu/api/res/1.2/Xdm96BfVJw4WV_W7GA7xLw--~C
                    /YXBwaWQ9eXNwb3J0cztjaD0yMzM2O2NyPTE7Y3c9MTc5MDtkeD04NTc7ZHk9MDtmaT11bGNyb3A7aD02MDtxPTEwMDt3PTQ
                    2/https://s.yimg.com/xe/i/us/sp/v/nfl_cutout/players_l/08202019/7200.2.png"
              },
              "is_undroppable": "0",
              "name": {
                "ascii_first": "Aaron",
                "ascii_last": "Rodgers",
                "first": "Aaron",
                "full": "Aaron Rodgers",
                "last": "Rodgers"
              },
              "player_id": "7200",
              "player_key": "331.p.7200",
              "player_notes_last_timestamp": 1568581740,
              "player_points": {
                "coverage_type": "season",
                "total": 359.14
              },
              "player_stats": {
                "coverage_type": "season",
                "stats": [
                  {
                    "stat": {
                      "stat_id": "4",
                      "value": "4381"
                    }
                  },
                  ...
                ]
              },
              "position_type": "O",
              "primary_position": "QB",
              "uniform_number": "12"
            })

        Returns:
            Player: YFPY Player instance.

        """
        if limit_to_league_stats:
            return self.query(
                f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/players;"
                f"player_keys={player_key}/stats",
                ["league", "players", "0", "player"],
                Player
            )
        else:
            return self.query(
                f"https://fantasysports.yahooapis.com/fantasy/v2/players;"
                f"player_keys={player_key}/stats",
                ["players", "0", "player"],
                Player
            )

    def get_player_stats_by_week(self, player_key: str, chosen_week: Union[int, str] = "current",
                                 limit_to_league_stats: bool = True) -> Player:
        """Retrieve stats of specific player by player_key and by week for chosen league.

        Args:
            player_key (str): The player key of chosen player (example: 331.p.7200 - <game_id>.p.<player_id>).
            chosen_week (int): Selected week for which to retrieve data.
            limit_to_league_stats (bool): Boolean (default: True) to limit the retrieved player stats to those for the
                selected league. When set to False, query retrieves all player stats for the game (NFL, NHL, NBA, MLB).

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_player_stats_by_week("331.p.7200", 1)
            Player({
              "bye_weeks": {
                "week": "9"
              },
              "display_position": "QB",
              "editorial_player_key": "nfl.p.7200",
              "editorial_team_abbr": "GB",
              "editorial_team_full_name": "Green Bay Packers",
              "editorial_team_key": "nfl.t.9",
              "eligible_positions": {
                "position": "QB"
              },
              "has_player_notes": 1,
              "headshot": {
                "size": "small",
                "url": "https://s.yimg.com/iu/api/res/1.2/Xdm96BfVJw4WV_W7GA7xLw--~C
                    /YXBwaWQ9eXNwb3J0cztjaD0yMzM2O2NyPTE7Y3c9MTc5MDtkeD04NTc7ZHk9MDtmaT11bGNyb3A7aD02MDtxPTEwMDt3PTQ
                    2/https://s.yimg.com/xe/i/us/sp/v/nfl_cutout/players_l/08202019/7200.2.png"
              },
              "is_undroppable": "0",
              "name": {
                "ascii_first": "Aaron",
                "ascii_last": "Rodgers",
                "first": "Aaron",
                "full": "Aaron Rodgers",
                "last": "Rodgers"
              },
              "player_id": "7200",
              "player_key": "331.p.7200",
              "player_notes_last_timestamp": 1568581740,
              "player_points": {
                "coverage_type": "week",
                "week": "1",
                "total": 10.56
              },
              "player_stats": {
                "coverage_type": "week",
                "week": "1",
                "stats": [
                  {
                    "stat": {
                      "stat_id": "4",
                      "value": "189"
                    }
                  },
                  ...
                ]
              },
              "position_type": "O",
              "primary_position": "QB",
              "uniform_number": "12"
            })

        Returns:
            Player: YFPY Player instance containing attribute "player_stats".

        """
        if limit_to_league_stats:
            return self.query(
                f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/players;"
                f"player_keys={player_key}/stats;type=week;week={chosen_week}",
                ["league", "players", "0", "player"],
                Player
            )
        else:
            return self.query(
                f"https://fantasysports.yahooapis.com/fantasy/v2/players;"
                f"player_keys={player_key}/stats;type=week;week={chosen_week}",
                ["players", "0", "player"],
                Player
            )

    def get_player_stats_by_date(self, player_key: str, chosen_date: str = None,
                                 limit_to_league_stats: bool = True) -> Player:
        """Retrieve player stats by player_key and by date for chosen league.

        Note:
            This applies to MLB, NBA, and NHL leagues, but does NOT apply to NFL leagues.
            This query will FAIL if you pass it an INVALID date string!

        Args:
            player_key (str): The player key of chosen player (example: 331.p.7200 - <game_id>.p.<player_id>).
            chosen_date (str): Selected date for which to retrieve data. REQUIRED FORMAT: YYYY-MM-DD (Ex. 2011-05-01)
            limit_to_league_stats (bool): Boolean (default: True) to limit the retrieved player stats to those for the
                selected league. When set to False, query retrieves all player stats for the game (NFL, NHL, NBA, MLB).

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_player_stats_by_date("nhl.p.4588", "2011-05-01")
            Player({
              "display_position": "G",
              "editorial_player_key": "nhl.p.4588",
              "editorial_team_abbr": "Was",
              "editorial_team_full_name": "Washington Capitals",
              "editorial_team_key": "nhl.t.23",
              "eligible_positions": {
                "position": "G"
              },
              "has_player_notes": 1,
              "headshot": {
                "size": "small",
                "url": "https://s.yimg.com/iu/api/res/1.2/CzntDh_d59voTqU6fhQy3g--~C/YXBwaWQ9eXNwb3J0cztjaD0yMzM2O2
                NyPTE7Y3c9MTc5MDtkeD04NTc7ZHk9MDtmaT11bGNyb3A7aD02MDtxPTEwMDt3PTQ2/https://s.yimg.com/
                xe/i/us/sp/v/nhl_cutout/players_l/10182019/4588.png"
              },
              "is_undroppable": "0",
              "name": {
                "ascii_first": "Braden",
                "ascii_last": "Holtby",
                "first": "Braden",
                "full": "Braden Holtby",
                "last": "Holtby"
              },
              "player_id": "4588",
              "player_key": "303.p.4588",
              "player_notes_last_timestamp": 1574133600,
              "player_stats": {
                "coverage_type": "date",
                "stats": [
                  {
                    "stat": {
                      "stat_id": "19",
                      "value": "1"
                    }
                  },
                  {
                    "stat": {
                      "stat_id": "22",
                      "value": "1"
                    }
                  },
                  {
                    "stat": {
                      "stat_id": "23",
                      "value": "1.00"
                    }
                  },
                  {
                    "stat": {
                      "stat_id": "25",
                      "value": "29"
                    }
                  },
                  {
                    "stat": {
                      "stat_id": "24",
                      "value": "30"
                    }
                  },
                  {
                    "stat": {
                      "stat_id": "26",
                      "value": ".967"
                    }
                  },
                  {
                    "stat": {
                      "stat_id": "27",
                      "value": "0"
                    }
                  }
                ]
              },
              "position_type": "G",
              "primary_position": "G",
              "uniform_number": "70"
            })

        Returns:
            Player: YFPY Player instnace containing attribute "player_stats".

        """
        if limit_to_league_stats:
            return self.query(
                f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/players;"
                f"player_keys={player_key}/stats;type=date;date={chosen_date}",
                ["league", "players", "0", "player"],
                Player
            )
        else:
            return self.query(
                f"https://fantasysports.yahooapis.com/fantasy/v2/players;"
                f"player_keys={player_key}/stats;type=date;date={chosen_date}",
                ["players", "0", "player"],
                Player
            )

    def get_player_ownership(self, player_key: str) -> Player:
        """Retrieve ownership of specific player by player_key for chosen league.

        Args:
            player_key (str): The player key of chosen player (example: 331.p.7200 - <game_id>.p.<player_id>).

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_player_ownership("331.p.7200")
            Player({
              "bye_weeks": {
                "week": "9"
              },
              "display_position": "QB",
              "editorial_player_key": "nfl.p.7200",
              "editorial_team_abbr": "GB",
              "editorial_team_full_name": "Green Bay Packers",
              "editorial_team_key": "nfl.t.9",
              "eligible_positions": {
                "position": "QB"
              },
              "has_player_notes": 1,
              "headshot": {
                "size": "small",
                "url": "https://s.yimg.com/iu/api/res/1.2/Xdm96BfVJw4WV_W7GA7xLw--~C
                    /YXBwaWQ9eXNwb3J0cztjaD0yMzM2O2NyPTE7Y3c9MTc5MDtkeD04NTc7ZHk9MDtmaT11bGNyb3A7aD02MDtxPTEwMDt3PTQ
                    2/https://s.yimg.com/xe/i/us/sp/v/nfl_cutout/players_l/08202019/7200.2.png"
              },
              "is_undroppable": "0",
              "name": {
                "ascii_first": "Aaron",
                "ascii_last": "Rodgers",
                "first": "Aaron",
                "full": "Aaron Rodgers",
                "last": "Rodgers"
              },
              "ownership": {
                "ownership_type": "team",
                "owner_team_key": "331.l.729259.t.4",
                "owner_team_name": "hold my D",
                "teams": {
                  "team": {
                    "clinched_playoffs": 1,
                    "draft_grade": "B-",
                    "draft_position": 1,
                    "draft_recap_url":
                        "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259/4/draftrecap",
                    "has_draft_grade": 1,
                    "league_scoring_type": "head",
                    "managers": {
                      "manager": {
                        "guid": "5KLNXUYW5RP22UMRKUXHBCIITI",
                        "manager_id": "4",
                        "nickname": "--hidden--"
                      }
                    },
                    "name": "hold my D",
                    "number_of_moves": "27",
                    "number_of_trades": "1",
                    "roster_adds": {
                      "coverage_type": "week",
                      "coverage_value": "17",
                      "value": "0"
                    },
                    "team_id": "4",
                    "team_key": "331.l.729259.t.4",
                    "team_logos": {
                      "team_logo": {
                        "size": "large",
                        "url": "https://ct.yimg.com/cy/1589/24677593583_68859308dd_192sq.jpg?ct=fantasy"
                      }
                    },
                    "url": "https://football.fantasysports.yahoo.com/archive/nfl/2014/729259/4",
                    "waiver_priority": 7
                  }
                }
              },
              "player_id": "7200",
              "player_key": "331.p.7200",
              "player_notes_last_timestamp": 1568581740,
              "position_type": "O",
              "primary_position": "QB",
              "uniform_number": "12"
            })

        Returns:
            Player: YFPY Player instance containing attribute "ownership".

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/players;"
            f"player_keys={player_key}/ownership",
            ["league", "players", "0", "player"],
            Player
        )

    def get_player_percent_owned_by_week(self, player_key: str, chosen_week: Union[int, str] = "current") -> Player:
        """Retrieve percent-owned of specific player by player_key and by week for chosen league.

        Args:
            player_key (str): The player key of chosen player (example: 331.p.7200 - <game_id>.p.<player_id>).
            chosen_week (int): Selected week for which to retrieve data.

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_player_percent_owned_by_week("331.p.7200", 1)
            Player({
              "bye_weeks": {
                "week": "9"
              },
              "display_position": "QB",
              "editorial_player_key": "nfl.p.7200",
              "editorial_team_abbr": "GB",
              "editorial_team_full_name": "Green Bay Packers",
              "editorial_team_key": "nfl.t.9",
              "eligible_positions": {
                "position": "QB"
              },
              "has_player_notes": 1,
              "headshot": {
                "size": "small",
                "url": "https://s.yimg.com/iu/api/res/1.2/Xdm96BfVJw4WV_W7GA7xLw--~C
                /YXBwaWQ9eXNwb3J0cztjaD0yMzM2O2NyPTE7Y3c9MTc5MDtkeD04NTc7ZHk9MDtmaT11bGNyb3A7aD02MDtxPTEwMDt3PTQ2/
                https://s.yimg.com/xe/i/us/sp/v/nfl_cutout/players_l/08202019/7200.2.png"
              },
              "is_undroppable": "0",
              "name": {
                "ascii_first": "Aaron",
                "ascii_last": "Rodgers",
                "first": "Aaron",
                "full": "Aaron Rodgers",
                "last": "Rodgers"
              },
              "percent_owned": {
                "coverage_type": "week",
                "week": "1",
                "value": 100,
                "delta": "0"
              },
              "player_id": "7200",
              "player_key": "331.p.7200",
              "player_notes_last_timestamp": 1568581740,
              "position_type": "O",
              "primary_position": "QB",
              "uniform_number": "12"
            })

        Returns:
            Player: YFPY Player instance containing attribute "percent_owned".

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/players;"
            f"player_keys={player_key}/percent_owned;type=week;week={chosen_week}",
            ["league", "players", "0", "player"],
            Player
        )

    def get_player_draft_analysis(self, player_key: str) -> Player:
        """Retrieve draft analysis of specific player by player_key for chosen league.

        Args:
            player_key (str): The player key of chosen player (example: 331.p.7200 - <game_id>.p.<player_id>).

        Examples:
            >>> from pathlib import Path
            >>> from yfpy.query import YahooFantasySportsQuery
            >>> query = YahooFantasySportsQuery(Path("/path/to/auth/directory"), league_id="######")
            >>> query.get_player_draft_analysis("331.p.7200")
            Player({
              "bye_weeks": {
                "week": "9"
              },
              "display_position": "QB",
              "draft_analysis": {
                "average_pick": "19.9",
                "average_round": "2.8",
                "average_cost": "38.5",
                "percent_drafted": "1.00"
              },
              "editorial_player_key": "nfl.p.7200",
              "editorial_team_abbr": "GB",
              "editorial_team_full_name": "Green Bay Packers",
              "editorial_team_key": "nfl.t.9",
              "eligible_positions": {
                "position": "QB"
              },
              "has_player_notes": 1,
              "headshot": {
                "size": "small",
                "url": "https://s.yimg.com/iu/api/res/1.2/Xdm96BfVJw4WV_W7GA7xLw--~C
                    /YXBwaWQ9eXNwb3J0cztjaD0yMzM2O2NyPTE7Y3c9MTc5MDtkeD04NTc7ZHk9MDtmaT11bGNyb3A7aD02MDtxPTEwMDt3PTQ
                    2/https://s.yimg.com/xe/i/us/sp/v/nfl_cutout/players_l/08202019/7200.2.png"
              },
              "is_undroppable": "0",
              "name": {
                "ascii_first": "Aaron",
                "ascii_last": "Rodgers",
                "first": "Aaron",
                "full": "Aaron Rodgers",
                "last": "Rodgers"
              },
              "player_id": "7200",
              "player_key": "331.p.7200",
              "player_notes_last_timestamp": 1568581740,
              "position_type": "O",
              "primary_position": "QB",
              "uniform_number": "12"
            })

        Returns:
            Player: YFPY Player instance containing attribute "draft_analysis".

        """
        return self.query(
            f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.get_league_key()}/players;"
            f"player_keys={player_key}/draft_analysis",
            ["league", "players", "0", "player"],
            Player
        )
