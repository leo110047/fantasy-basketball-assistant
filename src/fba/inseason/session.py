import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, RLock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import JsonValue, TypeAdapter

from fba.contracts.base import ConfigError, DataError, Record
from fba.contracts.inseason import (
    AdjustmentLedger,
    AuthorizationRequired,
    CalculationTimeout,
    EffectiveProjection,
    FeatureAvailability,
    FrozenPriors,
    InseasonParameters,
    InseasonPreferences,
    LeagueSnapshot,
    PlayerSnapshot,
    ProjectionRules,
    SyncState,
)
from fba.contracts.inseason_app import (
    ConfirmSettingsRequest,
    LeagueState,
    PlayerWorkspace,
    Sources,
    UserNotes,
)
from fba.contracts.yahoo import DiscoveredLeague, IdentityMappings, YahooCatalog
from fba.core.inseason import (
    availability,
    require_available,
    validate_inseason,
    validate_parameter_values,
)
from fba.data.codec import canonical, decode, digest
from fba.data.inseason_sources import AuthorizedFeed, projection_rules, read_priors
from fba.data.storage import Store, StoredSnapshot
from fba.data.yahoo import SyncBundle, YahooSync, discover
from fba.data.yahoo_auth import CredentialVault, SystemVault, YahooAuth, YahooReader
from fba.data.yahoo_identity import resolve_identities
from fba.data.yahoo_normalize import (
    differences,
    league_from_draft,
    normalize_league,
    settings_draft,
)
from fba.inseason.forecast_records import record_current_forecast
from fba.inseason.matchup import Simulation
from fba.inseason.projection import effective_projection
from fba.inseason.simulation_cache import reuse_simulation


def json_value(value: object) -> JsonValue:
    return TypeAdapter[JsonValue](JsonValue).validate_python(value)


def snapshot_record[T: Record](snapshot: StoredSnapshot, model: type[T]) -> T:
    return decode(model, json.dumps(snapshot.payload).encode(), "snapshot:" + snapshot.sha256)


class InseasonSession:
    def __init__(self, root: Path, defaults: Path, vault: CredentialVault | None = None) -> None:
        self.store = Store(root)
        self.lock = RLock()
        for name, model in (
            ("preferences", InseasonPreferences),
            ("parameters", InseasonParameters),
            ("yahoo-catalog", YahooCatalog),
        ):
            if not (root / f"{name}.json").exists():
                data = decode(
                    model, (defaults / f"{name}.json").read_bytes(), str(defaults / f"{name}.json")
                )
                self.store.write(f"{name}.json", data)
        self.preferences = self.store.read("preferences.json", InseasonPreferences)
        parameter_path = root / "parameters.json"
        raw = json.loads(parameter_path.read_bytes())
        if not isinstance(raw, dict):
            raise ConfigError("parameters.json: expected an object")
        installed = json.loads((defaults / "parameters.json").read_bytes())
        migrated = False
        for name in (
            "week_calibration",
            "calibration_minimum",
            "calibration_confidence_z",
            "prior_predictive",
            "weekly_exact_candidates",
            "lineup_batch",
            "scenario_cache_entries",
            "drop_shortlist",
        ):
            if name not in raw:
                raw[name] = installed[name]
                migrated = True
        self.params = decode(InseasonParameters, json.dumps(raw).encode(), str(parameter_path))
        if migrated:
            self.store.write("parameters.json", self.params)
        validate_parameter_values(self.params)
        try:
            ZoneInfo(self.preferences.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(
                f"preferences.json.timezone: unknown zone {self.preferences.timezone}"
            ) from None
        if self.preferences.stale_warning_seconds > self.preferences.stale_limit_seconds:
            raise ConfigError("preferences.json.stale_warning_seconds: exceeds stale limit")
        self.catalog = self.store.read("yahoo-catalog.json", YahooCatalog)
        installed_catalog = decode(
            YahooCatalog,
            (defaults / "yahoo-catalog.json").read_bytes(),
            str(defaults / "yahoo-catalog.json"),
        )
        # New provider labels must also reach an existing source installation.
        # Existing user mappings take precedence; do not rewrite their file.
        self.catalog = self.catalog.model_copy(
            update={
                "category_labels": {
                    **installed_catalog.category_labels,
                    **self.catalog.category_labels,
                },
                "stat_labels": {**installed_catalog.stat_labels, **self.catalog.stat_labels},
            }
        )
        self.auth = YahooAuth(vault or SystemVault("default"), self.params)
        self.reader = YahooReader(self.auth)
        self.cancelled = Event()
        self.reader.cancelled = self.cancelled
        self.discovered: tuple[DiscoveredLeague, ...] = ()
        self.progress = 0.0
        self.phase = "idle"
        self.last_error: str | None = None
        self.next_sync_at: datetime | None = None
        self.simulation_cache: dict[bool, tuple[str, Simulation]] = {}

    def league_store(self, key: str | None = None) -> Store:
        selected = key or self.preferences.selected_league
        if selected is None:
            raise DataError("league: 請先連結 Yahoo 並選擇聯盟")
        return Store(self.store.root / "leagues" / digest(selected.encode()))

    def state(self) -> LeagueState:
        return self.league_store().read("state.json", LeagueState)

    def save_state(self, state: LeagueState) -> None:
        self.league_store(state.selected.key).write("state.json", state)

    def identities(self) -> IdentityMappings:
        path = self.store.root / "identities.json"
        return (
            self.store.read("identities.json", IdentityMappings)
            if path.exists()
            else IdentityMappings(entries={})
        )

    def working_store(self) -> Store:
        return (
            self.league_store()
            if self.preferences.selected_league
            else Store(self.store.root / "players")
        )

    def workspace(self) -> PlayerWorkspace | None:
        store = self.working_store()
        return (
            store.read("workspace.json", PlayerWorkspace)
            if (store.root / "workspace.json").exists()
            else None
        )

    def ledger(self) -> AdjustmentLedger:
        store = self.working_store()
        return (
            store.read("ledger.json", AdjustmentLedger)
            if (store.root / "ledger.json").exists()
            else AdjustmentLedger(format_version=1, entries=())
        )

    def notes(self) -> UserNotes:
        store = self.working_store()
        return (
            store.read("notes.json", UserNotes)
            if (store.root / "notes.json").exists()
            else UserNotes(completed={}, ignored={}, adopted={})
        )

    def discover(self) -> tuple[DiscoveredLeague, ...]:
        self.discovered = discover(self.reader)
        return self.discovered

    def select(self, key: str) -> None:
        if not self.discovered:
            self.discover()
        selected = next((d for d in self.discovered if d.key == key), None)
        if selected is None:
            raise DataError("league.key: league was not returned by this Yahoo account")
        self.preferences = self.preferences.model_copy(update={"selected_league": key})
        self.store.write("preferences.json", self.preferences)
        if not (self.league_store().root / "state.json").exists():
            self.save_state(
                LeagueState(
                    selected=selected,
                    league=None,
                    draft=None,
                    confirmed_yahoo_settings=None,
                    confirmed_local_sha256=None,
                    bundle_sha256=None,
                    players_sha256=None,
                    priors_sha256=None,
                    normalized_sha256=None,
                    sources=None,
                    sync=SyncState(
                        connected=True,
                        authorization_valid=True,
                        last_success=None,
                        last_error=None,
                        settings_pending=True,
                        unresolved_rostered=(),
                    ),
                )
            )

    def synchronize(self) -> None:
        with self.lock:
            self.phase, self.progress, self.last_error = "Yahoo 同步", 0.0, None
            state = self.state()
            store = self.league_store()
            try:
                previous = (
                    snapshot_record(store.load_snapshot(state.bundle_sha256), SyncBundle)
                    if state.bundle_sha256
                    else None
                )
                timezone = (
                    state.league.timezone
                    if state.league
                    else self.catalog.confirmation_defaults.get("timezone")
                )
                if not isinstance(timezone, str):
                    raise ConfigError("Yahoo timezone: confirmed timezone is required")
                bundle = YahooSync(self.reader, store, self.params).sync(
                    state.selected,
                    previous,
                    timezone=timezone,
                )
                draft = settings_draft(bundle, state.selected, self.catalog)
                sha = store.snapshot(
                    "Yahoo normalized bundle",
                    datetime.fromisoformat(bundle.as_of),
                    json_value(bundle.model_dump(mode="json")),
                )
                changed = self.setting_differences(state, draft.document, draft.yahoo_settings)
                updated = state.model_copy(
                    update={
                        "draft": draft,
                        "bundle_sha256": sha,
                        "sync": state.sync.model_copy(
                            update={
                                "authorization_valid": True,
                                "settings_pending": bool(changed) or state.league is None,
                                "last_error": None,
                            }
                        ),
                    }
                )
                self.save_state(updated)
                if updated.league is not None:
                    self.publish_normalized(updated, bundle)
                if updated.sources is not None and updated.league is not None:
                    self.refresh_players()
                forecast_error = None
                try:
                    record_current_forecast(self)
                except (DataError, CalculationTimeout) as exc:
                    forecast_error = str(exc)
                latest = self.state()
                self.save_state(
                    latest.model_copy(
                        update={
                            "sync": latest.sync.model_copy(
                                update={"forecast_error": forecast_error}
                            )
                        }
                    )
                )
                self.phase, self.progress = "idle", 1.0
            except (
                DataError,
                CalculationTimeout,
                AuthorizationRequired,
                TimeoutError,
                ValueError,
            ) as exc:
                latest = self.state()
                self.last_error = str(exc)
                self.save_state(
                    latest.model_copy(
                        update={
                            "sync": latest.sync.model_copy(
                                update={
                                    "last_error": str(exc),
                                    "authorization_valid": False
                                    if isinstance(exc, AuthorizationRequired)
                                    else latest.sync.authorization_valid,
                                }
                            )
                        }
                    )
                )
                self.phase = "failed"
                raise

    def setting_differences(
        self, state: LeagueState, document: dict[str, JsonValue], yahoo: JsonValue
    ) -> JsonValue:
        if state.league is None:
            return [{"key": "league", "local": None, "yahoo": document}]
        if (
            state.confirmed_local_sha256 == digest(canonical(state.league))
            and state.confirmed_yahoo_settings == yahoo
        ):
            return []
        fields = set(document) - set(self.catalog.confirmation_defaults) - {"yahoo_settings_sha256"}
        local = {k: v for k, v in state.league.model_dump(mode="json").items() if k in fields}
        remote = {k: v for k, v in document.items() if k in fields}
        rows = (
            *differences(json_value(local), json_value(remote)),
            *differences(state.confirmed_yahoo_settings, yahoo, "Yahoo"),
        )
        return json_value([r.model_dump(mode="json") for r in rows])

    def confirm_settings(self, request: ConfirmSettingsRequest) -> None:
        with self.lock:
            state = self.state()
            if state.draft is None or state.bundle_sha256 is None:
                raise DataError("settings: synchronize Yahoo before confirming")
            league = (
                state.league if request.decision == "keep" else league_from_draft(request.document)
            )
            if league is None:
                raise DataError("settings: no existing league settings to retain")
            if league.league_id != state.selected.key or league.game_key != state.selected.game_key:
                raise DataError(
                    "settings: confirmed league identity must match selected Yahoo league"
                )
            validate_inseason(league, self.params)
            updated = state.model_copy(
                update={
                    "league": league,
                    "confirmed_yahoo_settings": state.draft.yahoo_settings,
                    "confirmed_local_sha256": digest(canonical(league)),
                    "sync": state.sync.model_copy(update={"settings_pending": False}),
                }
            )
            self.save_state(updated)
            bundle = snapshot_record(
                self.league_store().load_snapshot(state.bundle_sha256), SyncBundle
            )
            self.publish_normalized(updated, bundle)

    def publish_normalized(self, state: LeagueState, bundle: SyncBundle) -> None:
        if state.league is None:
            raise DataError("settings: confirmed league settings required")
        identities = self.identities()
        if state.players_sha256 is not None:
            players = snapshot_record(
                self.league_store().load_snapshot(state.players_sha256), PlayerSnapshot
            )
            resolved = resolve_identities(bundle, players, identities)
            if resolved != identities:
                self.store.write("identities.json", resolved)
            identities = resolved
        normalized = normalize_league(bundle, state.league, identities, self.catalog)
        sha = self.league_store().snapshot(
            "Yahoo league", normalized.as_of, json_value(normalized.model_dump(mode="json"))
        )
        self.save_state(
            state.model_copy(
                update={
                    "normalized_sha256": sha,
                    "sync": state.sync.model_copy(
                        update={
                            "last_success": normalized.as_of,
                            "unresolved_rostered": normalized.unresolved_rostered,
                        }
                    ),
                }
            )
        )

    def map_player(self, external: str, player: str) -> None:
        state = self.state()
        if state.players_sha256 is None:
            raise DataError("identity: load the NBA player source before assigning internal IDs")
        snapshot = snapshot_record(
            self.league_store().load_snapshot(state.players_sha256), PlayerSnapshot
        )
        if player not in {p.id for p in snapshot.players}:
            raise DataError(f"identity.{player}: unknown internal player ID")
        identities = self.identities()
        self.store.write(
            "identities.json",
            identities.model_copy(update={"entries": {**identities.entries, external: player}}),
        )
        if state.bundle_sha256 is not None and state.league is not None:
            self.publish_normalized(
                state,
                snapshot_record(self.league_store().load_snapshot(state.bundle_sha256), SyncBundle),
            )

    def configure_sources(self, sources: Sources) -> None:
        if self.preferences.selected_league is None:
            rules = projection_rules(Path(sources.forecast_path), self.catalog)
            workspace = self.load_player_workspace(sources, rules)
            self.working_store().write("workspace.json", workspace)
            return
        self.save_state(self.state().model_copy(update={"sources": sources}))
        self.refresh_players()

    def load_player_workspace(self, source: Sources, rules: ProjectionRules) -> PlayerWorkspace:
        priors = read_priors(
            Path(source.forecast_path), source.forecast_sha256, rules, source.forecast_known_at
        )
        players = AuthorizedFeed(
            source.player_url, source.permission_reference, self.params.request_timeout.value
        ).fetch()
        if players.season_id != rules.season_id:
            raise DataError("player_source.season_id: differs from selected projection season")
        store = self.working_store()
        prior_sha = store.snapshot(
            "pinned preseason projection",
            priors.known_at,
            json_value(priors.model_dump(mode="json")),
        )
        player_sha = store.snapshot(
            players.source, players.as_of, json_value(players.model_dump(mode="json"))
        )
        return PlayerWorkspace(
            sources=source, rules=rules, players_sha256=player_sha, priors_sha256=prior_sha
        )

    def refresh_players(self) -> None:
        state = self.state()
        if state.sources is None or state.league is None:
            raise DataError("sources: confirm league rules and configure player sources first")
        rules = ProjectionRules.model_validate(
            {k: getattr(state.league, k) for k in ProjectionRules.model_fields}
        )
        workspace = self.load_player_workspace(state.sources, rules)
        self.working_store().write("workspace.json", workspace)
        updated = state.model_copy(
            update={
                "players_sha256": workspace.players_sha256,
                "priors_sha256": workspace.priors_sha256,
            }
        )
        self.save_state(updated)
        if updated.bundle_sha256 is not None:
            self.publish_normalized(
                updated,
                snapshot_record(
                    self.league_store().load_snapshot(updated.bundle_sha256),
                    SyncBundle,
                ),
            )

    def player_inputs(self) -> tuple[ProjectionRules, PlayerSnapshot, FrozenPriors]:
        store = self.working_store()
        workspace = self.workspace()
        if workspace is not None:
            rules, players_sha, priors_sha = (
                workspace.rules,
                workspace.players_sha256,
                workspace.priors_sha256,
            )
        elif self.preferences.selected_league is not None:
            state = self.state()
            if state.league is None or state.players_sha256 is None or state.priors_sha256 is None:
                raise DataError("sources: 尚未載入賽季前預測與 NBA 球員資料")
            rules, players_sha, priors_sha = state.league, state.players_sha256, state.priors_sha256
        else:
            raise DataError("sources: 尚未載入賽季前預測與 NBA 球員資料")
        return (
            rules,
            snapshot_record(store.load_snapshot(players_sha), PlayerSnapshot),
            snapshot_record(store.load_snapshot(priors_sha), FrozenPriors),
        )

    def player_projection(
        self, now: datetime, ledger: AdjustmentLedger | None = None
    ) -> EffectiveProjection:
        rules, players, priors = self.player_inputs()
        return effective_projection(
            players,
            priors,
            ledger if ledger is not None else self.ledger(),
            rules,
            self.params,
            now,
            now.astimezone(ZoneInfo(rules.timezone)).date(),
        )

    def feature_availability(self, now: datetime) -> FeatureAvailability:
        state = self.state()
        players = (
            snapshot_record(self.league_store().load_snapshot(state.players_sha256), PlayerSnapshot)
            if state.players_sha256
            else None
        )
        return availability(
            state.sync, self.preferences, now, players_as_of=players.as_of if players else None
        )

    def simulation(
        self,
        *,
        ledger: AdjustmentLedger | None = None,
        require_league: bool = True,
        as_of: datetime | None = None,
        season: bool = False,
    ) -> Simulation:
        state = self.state()
        now = as_of or datetime.now(UTC)
        if state.league is None or state.players_sha256 is None or state.priors_sha256 is None:
            raise DataError("sources: 尚未載入已鎖定的賽季前預測與 NBA 球員資料")
        if state.normalized_sha256 is None:
            raise DataError("league: 尚無完整 Yahoo 聯盟快照")
        validate_inseason(state.league, self.params)
        store = self.league_store()
        players = snapshot_record(store.load_snapshot(state.players_sha256), PlayerSnapshot)
        if require_league:
            require_available(state.sync, self.preferences, now, players_as_of=players.as_of)
        priors = snapshot_record(store.load_snapshot(state.priors_sha256), FrozenPriors)
        league = snapshot_record(store.load_snapshot(state.normalized_sha256), LeagueSnapshot)
        simulation = Simulation(
            state.league,
            self.params,
            players,
            priors,
            ledger or self.ledger(),
            league,
            now,
            self.params.season_simulations.value if season else None,
            untouchable=self.preferences.untouchable,
        )
        simulation.cancelled = self.cancelled.is_set
        fits = store.history("acceptance-fits", limit=1)
        if fits:
            payload = fits[-1].payload
            if not isinstance(payload, dict):
                raise DataError("acceptance-fits: invalid fit report")
            values: dict[str, float] = {}
            for key in ("beta_rank", "beta_need", "threshold", "noise"):
                value = payload.get(key)
                if not isinstance(value, (float, int)):
                    raise DataError(f"acceptance-fits.{key}: numeric parameter required")
                values[key] = float(value)
            simulation.acceptance_fit = values
        return reuse_simulation(self.simulation_cache, season, simulation, state)
