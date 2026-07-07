# Project 5 — Mixtape Bug Hunt — Submission

## AI Usage

My guiding principle this project was: **AI is useful for explaining code I've already found, but unreliable for diagnosing bugs without full context.** Every time I asked the AI to "find the bug" before I'd read the relevant code myself, it pointed me somewhere plausible but wrong. So I used a deliberate workflow instead:

> **I find the suspicious code → AI helps me understand it → I verify the diagnosis by reading it myself.**

Concretely, here's how AI fit into each phase:

- **Orientation (explaining, not diagnosing).** I had Claude Code (Opus 4.8) read the tree and lay out the route → service call chains, which is what the codebase map below is built from. I read every file myself alongside its summary — the AI was summarizing code I was also reading, never a substitute for reading it.
- **Understanding suspicious code, once I'd located it.** After I'd narrowed a bug to a specific function, I used the AI the way it's actually reliable:
  - *Edge-case probing:* I gave it the streak function I'd already flagged and asked "what edge cases could cause this to return the wrong value?" — which surfaced the day-of-week guard as the suspect branch.
  - *Targeted factual questions:* once I'd narrowed Issue #1 to a date comparison, I asked "what's the difference between Python's `datetime.weekday()` and `isoweekday()`?" and confirmed `weekday()` returns 6 for Sunday (Monday=0). That's a fact I could then check directly, not a diagnosis I had to trust.
  - *Structural diffs:* for Issue #4 I gave it the two similar notification paths (`add_to_playlist` vs `rate_song`) and asked "what's the structural difference between these two blocks?" — which pinned down that `rate_song` simply never calls `create_notification`.
- **Verification (always me, empirically).** I never let a plausible AI explanation stand as the answer. I confirmed each diagnosis by reading the code and by running throwaway snippets / the test suite: rate a song and count notifications (#4), insert a "yesterday 23:00" event and check the feed (#2), `COUNT(*)` the raw join rows (#3).
- **Where verifying-myself saved me from a wrong AI answer (Issue #3).** The AI's first read of `search_service.py` — matching the bug report and the test comment — was "the `outerjoin` on `song_tags` fans out one row per tag, so a 3-tag song returns 3 times." Plausible, and half-right. But when I actually ran `search_songs("Anthem")` it returned the song **once** and the search tests **passed**. Reading further, I found the cause: the legacy `db.session.query()` API auto-de-duplicates full ORM entities in SQLAlchemy 2.0, so the fan-out is real at the SQL level (`COUNT(*)` = 3) but invisible through this API. The AI had confidently described a user-visible bug that doesn't reproduce here — exactly the "plausible but wrong" failure mode of asking AI to diagnose without full context. Only reading and running it myself caught it. I document this per-bug in the Issue #3 RCA as well.

Net: AI was a strong *explainer* of code I'd already located and a good generator of reproduction scaffolding, but the diagnosis and every fix decision came from me reading and running the code. The one time I could have taken an AI diagnosis at face value (#3), it would have been wrong.

---

## Codebase Map

Mixtape is a small Flask + SQLAlchemy JSON API. There is no front-end and no auth — every endpoint takes IDs directly. The architecture is a strict three-layer split: **routes** parse/validate input and format JSON responses, **services** hold all business logic, **models** define the schema. Routes never touch the DB directly (except the trivial `GET /users/<id>` lookup); they delegate immediately to a service function. This is the single most important organizing pattern: **if an endpoint misbehaves, the bug is in the service it calls, not the route.**

### Main files and their roles

- **`app.py`** — Flask application factory (`create_app`). Creates the shared `db = SQLAlchemy()` instance, configures the SQLite URI, registers the four blueprints under URL prefixes (`/songs`, `/playlists`, `/users`, `/feed`), and calls `db.create_all()`.

- **`models.py`** — Defines 6 entity models plus 3 association tables:
  - `User` — carries `listening_streak` and `last_listened_at` directly on the row (no separate streak table). Has a self-referential many-to-many `friends` relationship via the `friendships` table.
  - `Song` — shared by a user (`shared_by` FK). Tags via `song_tags` join table (`lazy="subquery"`).
  - `ListeningEvent` — one row per play, with `listened_at`. This is the raw data behind both streaks and the "listening now" feed.
  - `Rating` — 1–5 score, unique per (user, song). The rating is its own table (not a column on `Song`).
  - `Playlist` — songs via the `playlist_entries` association table, which crucially carries an extra **`position` integer** column — songs have an explicit order, not just insertion order — plus `added_by` and `added_at`.
  - `Notification` — a `user_id` recipient, a `notification_type` string, and a `body`. Created as a side effect of friend interactions.

- **`routes/`** — one blueprint per resource. Each handler pulls params from the request, calls one service function, and jsonifies the result (translating `ValueError` into a 400/404).
  - `songs.py` — `/songs/search`, `/songs/<id>`, `POST /songs/<id>/rate`, `POST /songs/<id>/listen`
  - `playlists.py` — `POST /playlists/`, `GET /playlists/<id>`, `GET/POST /playlists/<id>/songs`
  - `users.py` — `GET /users/<id>`, `/users/<id>/streak`, `/users/<id>/notifications`, `POST .../read`
  - `feed.py` — `/feed/<id>/listening-now`, `/feed/<id>/activity`

- **`services/`** — where all logic (and all five bugs) live: `streak_service`, `feed_service`, `search_service`, `notification_service`, `playlist_service`.

- **`seed_data.py`** — drops and recreates the DB with 5 users, 5 friendships, 13 songs (deliberately including songs with 0, 1, and 3+ tags to exercise the search path), 3 playlists of 5–7 positioned songs, recent + old listening events, streak state, and one existing playlist-add notification.

### Data flow — worked example: "a friend rates my song" (Issue #4 feature)

1. Client sends `POST /songs/<song_id>/rate` with JSON `{user_id, score}`.
2. `routes/songs.py::rate()` validates that `user_id` and `score` are present, casts `score` to `int`, and calls `notification_service.rate_song(user_id, song_id, score)`.
3. `rate_song()` validates the 1–5 range, loads the `Song` and rater `User`, then either updates the existing `Rating` (unique per user+song) or inserts a new one, and commits.
4. It returns the `Rating`; the route serializes it with `.to_dict()` and returns `201`.

Compare this to the *working* notification path, `add_to_playlist()` in the same service: after mutating state it calls `create_notification(user_id=song.shared_by, ...)` to alert the original sharer. `rate_song()` has no equivalent call — which is exactly Issue #4.

### Data flow — worked example: "view a playlist"

`GET /playlists/<id>/songs` → `routes/playlists.py::get_songs()` → `playlist_service.get_playlist_songs(id)`, which joins `Song` against `playlist_entries`, filters to the playlist, and orders by `playlist_entries.position` ascending. The route wraps the list as `{"songs": [...], "count": n}`.

### Patterns worth noting

- **Every route delegates to exactly one service function.** Input parsing and response shaping live in routes; logic lives in services.
- **Time is stored in UTC** via `datetime.now(timezone.utc)`, but `last_listened_at` can come back from SQLite tz-naive, so `streak_service` defensively re-attaches `timezone.utc`.
- **Notifications are a side effect**, created inline inside the service method that performs the triggering action — there's no event bus. This makes "missing notification" bugs a matter of a missing call, not a broken listener.
- **Association tables carry data** (`playlist_entries.position`, `added_by`, `added_at`) — they're not pure join tables.

---

## Root Cause Analysis

### Issue #1 — My listening streak keeps resetting

**How I reproduced it.** The report said it only happens on Sundays. There is already a test for exactly this scenario — `tests/test_streaks.py::test_streak_increments_on_sunday` — which listens on Saturday (2024-06-15) then Sunday (2024-06-16) and asserts the streak becomes 2. Running `pytest tests/test_streaks.py` showed that test failing with `assert 1 == 2`: the streak reset to 1 on the Sunday listen instead of incrementing. That reproduces kenji's "12 → 1 on Sunday morning" exactly.

**How I found the root cause.** The endpoint is `GET /users/<id>/streak` (`routes/users.py`), but the streak is *written* on `POST /songs/<id>/listen` → `streak_service.record_listening_event()` → `update_listening_streak()`. I read `update_listening_streak` and the increment branch jumped out:
```python
elif days_since_last == 1 and today.weekday() != 6:
    user.listening_streak += 1
```
The moment of certainty was checking what `date.weekday()` returns: **Monday=0 … Sunday=6**. So `today.weekday() != 6` is `False` precisely when today is Sunday.

**The root cause.** The consecutive-day branch had an extra, incorrect guard `and today.weekday() != 6`. `weekday()` returns 6 for Sunday, so on any Sunday the "listened yesterday → increment" branch was skipped and execution fell through to the `else`, which resets the streak to 1. Listening on a Sunday after a Saturday (a genuinely consecutive day) was therefore treated as a skipped day. There was no legitimate reason for a weekday check at all — a streak counts consecutive calendar days regardless of which day of the week it is.

**My fix and side-effect check.** I removed the `and today.weekday() != 6` clause so the branch is simply `elif days_since_last == 1:`. I verified both sides of the boundary via the existing suite: `test_streak_increments_on_consecutive_day` (Mon→Tue) still passes, `test_streak_does_not_double_count_same_day` (`days_since_last == 0` returns early) still passes, and `test_streak_resets_after_skipped_day` (Mon→Wed, `days_since_last == 2` → `else`) still passes — so a real skipped day still resets. All 5 streak tests pass.

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it.** `tests/test_playlists.py::test_playlist_returns_all_songs` seeds a playlist with 5 positioned songs and asserts `get_playlist_songs` returns 5. It failed returning 4, and `test_playlist_returns_songs_in_order` failed because the list ended at "Track 4". That matches darius's report: a 7-song playlist shows 6, and it's always the most recently added (highest `position`) one missing.

**How I found the root cause.** `GET /playlists/<id>/songs` → `routes/playlists.py::get_songs()` → `playlist_service.get_playlist_songs()`. The query itself was correct — join `Song` to `playlist_entries`, filter by playlist, order by `position` ascending. But the very last line was:
```python
return [song.to_dict() for song in songs[:-1]]
```
The `[:-1]` slice was the smoking gun. The function docstring even claims "This function returns all songs in the playlist," which the code contradicts.

**The root cause.** The result list was sliced with `[:-1]`, which drops the final element. Because the query orders by `position` ascending, the final element is always the highest-position (most recently added) song — so the newest song is silently omitted every time. When another song is added, the previously-last song is no longer last, so it reappears, and the brand-new one becomes the dropped element. That's exactly the "adding a song frees the previous one and hides the new one" behavior darius described.

**My fix and side-effect check.** I removed the `[:-1]` slice: `return [song.to_dict() for song in songs]`. I checked the empty-playlist boundary (`test_empty_playlist_returns_empty_list`): with the old code `[][:-1]` also happened to yield `[]`, so an empty playlist was never the visible symptom, and it still returns `[]` after the fix. All 3 playlist tests pass, and ordering is preserved.

### Issue #4 — Notified on playlist-add but not on rating

**How I reproduced it.** There's no test for this, so I wrote a snippet against the seeded DB: load a song shared by simone, have kenji `rate_song(kenji, song, 5)`, then `get_notifications(simone)`. Before the fix it printed `notifications: 0` even though the `Rating` row was confirmed saved. That matches aaliya's report — rating persists (shows on the song), but no notification is ever created.

**How I found the root cause.** `POST /songs/<id>/rate` → `routes/songs.py::rate()` → `notification_service.rate_song()`. The two notification-producing actions live side by side in the same file, which made the comparison obvious. `add_to_playlist()` (the path that *works*) ends with:
```python
if song.shared_by != added_by_user_id:
    create_notification(user_id=song.shared_by, notification_type="song_added_to_playlist", ...)
```
`rate_song()` saved the `Rating`, committed, and returned — with no analogous `create_notification` call anywhere. The `create_notification` helper and the `song_rated` type were already documented in the module docstring, so the notification was clearly *intended* and simply never wired up.

**The root cause.** `rate_song()` was missing the notification side effect entirely. It's not a broken listener or a filtering bug — the code that would create the "X rated your song" notification was never written into the rating path, unlike the playlist path. So ratings silently produced zero notifications for anyone.

**My fix and side-effect check.** After the rating is committed, I added a `create_notification(user_id=song.shared_by, notification_type="song_rated", body="… rated your song … N stars.")` call, guarded by `if song.shared_by != user_id` so users don't get notified for rating their own song — mirroring the exact self-action guard used by `add_to_playlist`. I verified: (a) a cross-user rating now produces exactly one `song_rated` notification with the right body; (b) a self-rating produces zero (delta 0); (c) the function still returns the `Rating`, so `routes/songs.py` still serializes a `201` correctly; (d) the re-rating branch (updating an existing score) still commits and now also notifies, which is the desired behavior. The playlist-add notification path is untouched and still works.

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it.** No test exists, so I reproduced it with a snippet. `datetime.now()` at runtime was 2026-07-07. I cleared the seeded listening events (they contaminate the test — the seed gives darius a "10 minutes ago" event, so he'd appear legitimately regardless), then inserted one event for a friend at **yesterday 23:00** and queried `get_friends_listening_now`. The friend appeared, even though their only listen was the previous calendar evening — reproducing nova's report that darius's 11pm listen still shows at 9am the next day. The event was ~23–24h old, i.e. still inside the rolling window.

**How I found the root cause.** `GET /feed/<id>/listening-now` → `routes/feed.py::listening_now()` → `feed_service.get_friends_listening_now()`. The recency filter was:
```python
RECENT_THRESHOLD = timedelta(hours=24)
cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD
```
The `filter(ListeningEvent.listened_at >= cutoff)` is a **rolling 24-hour window**, not a "today" boundary. That's the mismatch: the feature is meant to show who listened *today*, but a rolling window keeps last night's plays alive until the same clock time the next day.

**The root cause.** The cutoff was computed as "now minus 24 hours" instead of "the start of today." A listen at 23:00 yesterday is still `>= now - 24h` for the entire morning and afternoon of the next day, so previous-calendar-day events leaked into "listening now" until ~23:00 the following day. The window boundary was time-relative when it needed to be calendar-relative.

**My fix and side-effect check.** I replaced `RECENT_THRESHOLD` with a small helper `_start_of_today(now)` that returns midnight UTC of the current day, and set `cutoff = _start_of_today(datetime.now(timezone.utc))`. I removed the now-unused `timedelta` import. I verified **both sides of the midnight boundary** in isolation: a friend whose only listen was yesterday 23:00 no longer appears (expected), while a friend who listened today at 00:30 does appear (expected). I confirmed `get_activity_feed` in the same file is unaffected — it never used `RECENT_THRESHOLD` and is documented as intentionally *not* recency-filtered. (Note: this uses UTC calendar days, consistent with how the rest of the app stores and compares timestamps; a true per-user-timezone "today" would require timezone data the app doesn't track.)

### Issue #3 — The same song shows up twice/three times in search

**How I reproduced it — and the surprise.** I expected this to be the easiest one, but it is the most interesting. I ran `search_songs("Anthem")` against the seeded DB (Crown Heights Anthem has 3 tags) expecting 3 results — and got **1**. The four search tests, including `test_search_no_duplicates_multi_tag_song` whose comment literally says *"Should be 1, bug causes it to be 3"*, all **passed** unmodified. So in this environment the reported bug does **not** reproduce. Rather than "fix" something I couldn't observe, I dug into *why*.

**How I found the root cause.** `GET /songs/search` → `routes/songs.py::search()` → `search_service.search_songs()`. The query was:
```python
db.session.query(Song).outerjoin(song_tags, Song.id == song_tags.c.song_id).filter(...).all()
```
The `outerjoin` against the `song_tags` association table is the classic duplication mechanism: a join to a one-to-many produces one row per (song, tag) pair, so a 3-tag song yields 3 rows. I confirmed this is real at the SQL level — a `COUNT(*)` over that exact join for "Anthem" returns **3 rows**. So the fan-out genuinely happens in the database.

The reason it doesn't surface: this code uses the **legacy `db.session.query(Song)` API**, and SQLAlchemy 2.0's legacy `Query` automatically **uniquifies full ORM entities by primary-key identity** before returning them from `.all()`. The three raw rows collapse back to one `Song` object. (Had the code used the 2.0-style `select(Song)` with `session.execute(...).scalars()`, which does *not* auto-unique unless you call `.unique()`, the duplicates would appear — that's almost certainly the environment the bug was filed against.)

**The root cause.** The query joins `song_tags` even though no filter or selected column uses it — tags are already loaded through the `Song.tags` relationship in `to_dict()`. That pointless join fans each song out to one row per tag. Whether the user *sees* duplicates then depends entirely on an implementation detail of the query API (legacy `Query` de-dupes; 2.0 `select` does not). So the code is latently wrong regardless of whether it happens to display correctly today.

**My fix and side-effect check.** I removed the `outerjoin(song_tags, ...)` entirely (and the now-unused `Tag`/`song_tags` imports). Filtering on `Song.title`/`Song.artist` needs no join, so the query now returns exactly one row per matching song *by construction* — it no longer relies on the ORM's uniquing to be correct, which makes it robust if this code is ever ported to the 2.0 `select()` style. I re-ran all search tests (still pass), confirmed `search_songs("Anthem")` still returns 1, and confirmed the raw-SQL fan-out is gone. Because I could not reproduce a user-visible failure in this environment, I count this as a correctness/robustness fix and am transparent that its *observable* impact here is nil — the duplication was a latent defect masked by the legacy Query API.


