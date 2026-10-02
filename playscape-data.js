/* RAWG catalog and CheapShark price UI. API credentials never enter this file. */
(function () {
  "use strict";
  var previousOpenGame = window.openGame;
  var dealsPage = 1;
  var dealsPages = 0;
  var dealTimer = null;
  var gallery = [];
  var galleryIndex = 0;
  var routeDeals = location.pathname.replace(/\/$/, "") === "/deals";
  var routeGames = location.pathname.replace(/\/$/, "") === "/games";
  var rawgKeyConfigured = false;
  var catalogPollTimer = null;
  var catalogPollAttempts = 0;
  var catalogModeButton = document.getElementById("catalogModeBtn");
  if (!routeDeals) remoteCatalogMode = true;

  function text(tag, className, value) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined && value !== null) node.textContent = String(value);
    return node;
  }
  function validUrl(value) {
    try {
      var parsed = new URL(String(value || ""));
      return parsed.protocol === "https:" || parsed.protocol === "http:";
    } catch (_) { return false; }
  }
  function rawgGame(raw) {
    var id = Number(raw.app_id !== undefined ? raw.app_id : raw.id);
    var genres = Array.isArray(raw.genres) ? raw.genres : [];
    var platforms = Array.isArray(raw.platforms) ? raw.platforms.map(function (item) {
      return typeof item === "string" ? item : item && item.name;
    }).filter(Boolean) : [];
    var rating = raw.rating === null || raw.rating === undefined || raw.rating === "" ? null : Number(raw.rating);
    var release = raw.released || raw.release_date || "";
    var date = release ? new Date(release + "T00:00:00") : null;
    var releaseLabel = date && !Number.isNaN(date.getTime()) ? date.toLocaleDateString(undefined, {year:"numeric",month:"short",day:"numeric"}) : "Release date unavailable";
    var developers = Array.isArray(raw.developers) ? raw.developers.filter(Boolean) : [];
    var publishers = Array.isArray(raw.publishers) ? raw.publishers.filter(Boolean) : [];
    var stores = Array.isArray(raw.stores) ? raw.stores : [];
    var requirements = Array.isArray(raw.system_requirements) ? raw.system_requirements : [];
    return {
      id: String(id), app: id, title: raw.name || "Untitled game",
      art: validUrl(raw.background_image) ? raw.background_image : (validUrl(raw.cover_image) ? raw.cover_image : ""),
      genres: genres, genreLabel: genres.slice(0, 2).join(" · ") || "Game",
      release: release, releaseLabel: releaseLabel,
      developer: developers.join(", ") || "Not listed",
      publisher: publishers.join(", ") || "Not listed",
      platform: platforms.join(" · ") || "Not listed",
      players: "Not supplied by RAWG",
      review: rating === null || Number.isNaN(rating) ? "RAWG rating unavailable" : "RAWG " + rating.toFixed(1) + "/5",
      reviewCount: Number(raw.ratings_count || 0),
      description: raw.description || raw.short_description || "Description not available from RAWG.",
      shortDescription: raw.short_description || "",
      verdict: "GAMEVAULT has not published an editorial review for this game.",
      steam: validUrl(raw.steam_url) ? raw.steam_url : null,
      min: raw.min_requirements || null,
      rec: raw.rec_requirements || null,
      requirements: requirements,
      rating: rating,
      metacritic: raw.metacritic,
      ratingsCount: Number(raw.ratings_count || 0),
      rawgData: raw,
      steamData: raw,
      source: "RAWG",
      screenshots: Array.isArray(raw.screenshots) ? raw.screenshots : [],
      trailers: Array.isArray(raw.trailers) ? raw.trailers : [],
      stores: stores,
      similarGames: Array.isArray(raw.similar_games) ? raw.similar_games : []
    };
  }
  window.normalizeSteamGame = rawgGame;
  window.rememberSteamGames = function (items) {
    try {
      var old = JSON.parse(localStorage.getItem("playscape-steam-cache") || "[]");
      var byId = new Map(old.map(function (item) { return [String(item.app_id), item]; }));
      (items || []).forEach(function (item) { byId.set(String(item.app_id), item); });
      localStorage.setItem("playscape-steam-cache", JSON.stringify(Array.from(byId.values()).slice(-500)));
    } catch (_) {}
  };

  function shelfCard(raw) {
    var game = rawgGame(raw);
    var card = text("article", "steam-mini-card");
    if (game.art) {
      var image = text("img");
      image.loading = "lazy";
      image.src = game.art;
      image.alt = game.title + " artwork";
      image.onerror = function () { image.remove(); };
      card.append(image);
    }
    var copy = text("div", "steam-mini-copy");
    copy.append(text("strong", "", game.title));
    copy.append(text("span", "", game.review + " · " + game.releaseLabel));
    copy.append(text("span", "", game.genres.slice(0, 2).join(" · ") || "RAWG game"));
    var open = text("button", "", "Explore this game ↗");
    open.type = "button";
    open.dataset.open = game.id;
    copy.append(open);
    card.append(copy);
    return card;
  }

  window.normalizeSteamGame = rawgGame;
  window.compareSummary = function (game) {
    var minimum = game.min ? (typeof game.min === "string" ? game.min : Object.values(game.min).join(" · ")) : "Not available";
    return {
      Genre: game.genreLabel || "Not available",
      Release: game.releaseLabel || "Not available",
      Developer: game.developer || "Not available",
      Publisher: game.publisher || "Not available",
      Platforms: game.platform || "Not available",
      "RAWG rating": game.rating === null || game.rating === undefined ? "Not available" : game.rating.toFixed(1) + " / 5",
      Metacritic: game.metacritic === null || game.metacritic === undefined ? "Not available" : game.metacritic,
      "PC minimum": minimum
    };
  };

  window.configureSortOptions = function (fullCatalog) {
    var select = document.getElementById("sortSelect");
    select.innerHTML = fullCatalog
      ? '<option value="popular">Popular</option><option value="most-reviewed">Most reviewed</option><option value="recently-released">Recently released</option><option value="highest-rated">Highest RAWG rating</option><option value="metacritic">Metacritic</option><option value="az">Name: A–Z</option><option value="za">Name: Z–A</option>'
      : '<option value="featured">GAMEVAULT picks</option><option value="newest">Newest release</option><option value="az">Name: A–Z</option>';
    if (!Array.from(select.options).some(function (item) { return item.value === select.value; })) select.value = fullCatalog ? "popular" : "featured";
  };
  function pollForCatalog() {
    if (catalogPollTimer || catalogPollAttempts >= 90) return;
    catalogPollTimer = setTimeout(async function () {
      catalogPollTimer = null;
      catalogPollAttempts += 1;
      try {
        var state = await apiRequest("/api/catalog/status");
        if (Number(state.catalog_games || 0) > 0) {
          catalogPollAttempts = 0;
          window.loadRemoteCatalog();
          return;
        }
        if (state.status === "failed") {
          catalogPollAttempts = 0;
          window.refreshCatalogStatus();
          return;
        }
        if (catalogPollAttempts < 90 && rawgKeyConfigured) pollForCatalog();
      } catch (_) {}
    }, 1800);
  }
  window.remoteQueryString = function () {
    var params = new URLSearchParams({catalog:"rawg",page:String(remotePage),limit:"24",sort:sortSelect.value});
    var query = searchInput.value.trim();
    if (query) params.set("q", query);
    if (activeGenre !== "All") params.set("genre", activeGenre);
    var platform = document.getElementById("platformFilter").value;
    var score = document.getElementById("priceFilter").value;
    var release = document.getElementById("releaseFilter").value;
    var rating = document.getElementById("ratingFilter").value;
    var tag = document.getElementById("tagFilter").value;
    var developer = document.getElementById("developerFilter").value.trim();
    var publisher = document.getElementById("publisherFilter").value.trim();
    if (platform) params.set("platform", platform);
    if (score) params.set("metacritic_min", score);
    if (release) params.set("release", release);
    if (rating) params.set("rating_min", rating);
    if (tag) params.set("tag", tag);
    if (developer) params.set("developer", developer);
    if (publisher) params.set("publisher", publisher);
    return params;
  };
  window.updateCatalogUrl = function () {
    if (!remoteCatalogMode) return;
    history.replaceState({}, "", location.pathname + "?" + window.remoteQueryString().toString() + location.hash);
  };
  window.setCatalogMode = function (enabled) {
    remoteCatalogMode = true;
    wishlistOnly = false;
    remotePage = 1;
    window.configureSortOptions(remoteCatalogMode);
    setRemoteFiltersEnabled(remoteCatalogMode && remoteTotal > 0);
    document.getElementById("catalogPagination").hidden = true;
    if (catalogModeButton) catalogModeButton.textContent = "Browse the RAWG catalog ↗";
    window.loadRemoteCatalog();
  };
  window.findCompareGame = function (id) {
    var local = games.find(function (game) { return String(game.id) === String(id); });
    if (local) return local;
    var rows = (remoteResults || []).concat(steamShelfRecords || []);
    try { rows = rows.concat(JSON.parse(localStorage.getItem("playscape-steam-cache") || "[]")); } catch (_) {}
    var raw = rows.find(function (game) { return String(game.app_id) === String(id); });
    return raw ? rawgGame(raw) : null;
  };

  window.refreshCatalogStatus = async function () {
    try {
      var state = await apiRequest("/api/catalog/status");
      rawgKeyConfigured = !!state.rawg_key_configured;
      if (!routeDeals) remoteCatalogMode = true;
      window.configureSortOptions(remoteCatalogMode && state.catalog_games > 0);
      setRemoteFiltersEnabled(remoteCatalogMode && state.catalog_games > 0);
      if (state.catalog_games > 0) {
        sourceNotice(state.catalog_games.toLocaleString() + " RAWG games available · " + state.detailed_games.toLocaleString() + " with expanded details · Catalog " + (state.status === "running" ? "updating" : "ready") + ".", state.status === "failed" ? "error" : "live");
        document.getElementById("heroShowcase").hidden = true;
        if (catalogModeButton) catalogModeButton.textContent = "Browse the RAWG catalog ↗";
        if (remoteCatalogMode) document.getElementById("libraryCount").textContent = state.catalog_games.toLocaleString() + " RAWG games indexed";
        await Promise.all([window.loadSteamShelves(), window.loadSteamGenres()]);
        if (state.status === "failed") sourceNotice("RAWG catalog needs attention: " + (state.last_error || state.message || "The request could not be completed."), "error");
      } else {
        document.getElementById("heroShowcase").hidden = true;
        document.getElementById("steamShelves").hidden = true;
        document.getElementById("upcoming").hidden = true;
        if (state.status === "failed") {
          var syncError = state.last_error || state.message || "RAWG could not complete the request.";
          sourceNotice(syncError, "error");
          document.getElementById("libraryCount").textContent = "RAWG catalog unavailable";
        } else {
          sourceNotice(rawgKeyConfigured
            ? "RAWG is loading games automatically. Your catalog will appear here in a moment."
            : "RAWG_API_KEY is not available to the game server.", rawgKeyConfigured ? "live" : "error");
          document.getElementById("libraryCount").textContent = rawgKeyConfigured ? "Loading RAWG games…" : "RAWG catalog unavailable";
          if (rawgKeyConfigured) pollForCatalog();
        }
      }
      return state;
    } catch (_) {
      sourceNotice("The GAMEVAULT API is offline. Start the server to browse the synchronized game catalog, prices and account wishlist.", "error");
      document.getElementById("heroShowcase").hidden = true;
      document.getElementById("steamShelves").hidden = true;
      document.getElementById("upcoming").hidden = true;
      grid.innerHTML = '<div class="empty-state"><div><strong>The RAWG catalog is offline.</strong>Start the GAMEVAULT server to browse synchronized games.</div></div>';
      return null;
    }
  };

  window.loadSteamShelves = async function () {
    try {
      var data = await apiRequest("/api/home");
      var shelves = data.shelves || {};
      var sections = [];
      if (shelves.trending && shelves.trending.length) sections.push({title:"Trending now",note:"Popular games from RAWG.",results:shelves.trending});
      if (shelves.recentlyReleased && shelves.recentlyReleased.length) sections.push({title:"Recently released",note:"Release dates from RAWG.",results:shelves.recentlyReleased});
      if (shelves.mostRated && shelves.mostRated.length) sections.push({title:"Most highly rated",note:"Player ratings from RAWG.",results:shelves.mostRated});
      (shelves.genres || []).forEach(function (item) {
        if (item.games && item.games.length) sections.push({title:item.name,note:"Games in the " + item.name + " genre.",genre:item.name,results:item.games});
      });
      steamShelfRecords = sections.reduce(function (all, section) { return all.concat(section.results); }, []);
      window.rememberSteamGames(steamShelfRecords);
      document.getElementById("steamShelfSource").textContent = sections.length + " database shelves";
      var host = document.getElementById("steamShelfRows");
      host.innerHTML = "";
      sections.forEach(function (section) {
        var shelf = text("section", "steam-shelf");
        var heading = text("div", "steam-shelf-head");
        var copy = text("div");
        copy.append(text("h3", "", section.title), text("p", "", section.note + " Showing up to 15 games."));
        heading.append(copy);
        var browse = text("button", "text-link", "View all ↗");
        browse.type = "button";
        browse.addEventListener("click", function () {
          activeGenre = section.genre || "All";
          document.querySelectorAll(".chip").forEach(function (chip) { chip.classList.toggle("active", chip.dataset.genre === activeGenre); });
          window.setCatalogMode(true);
          document.getElementById("library").scrollIntoView({behavior:"smooth"});
        });
        heading.append(browse);
        var cards = text("div", "steam-shelf-grid");
        section.results.slice(0, 15).forEach(function (raw) { cards.append(shelfCard(raw)); });
        shelf.append(heading, cards);
        host.append(shelf);
      });
      document.getElementById("steamShelves").hidden = !sections.length;
      var heroRows = sections.reduce(function (all, section) { return all.concat(section.results); }, []).slice(0, 5);
      if (heroRows.length) {
        featuredHeroes = heroRows.map(function (raw) {
          var game = rawgGame(raw);
          return {id:game.id,app:game.app,title:game.title,genre:game.genreLabel,review:game.review,style:game.platform,art:game.art,background:raw.background_image || game.art};
        });
        var dots = document.getElementById("heroDots");
        dots.innerHTML = "";
        featuredHeroes.forEach(function (item, index) {
          var button = text("button", "hero-dot" + (index ? "" : " active"));
          button.dataset.slide = String(index);
          button.setAttribute("aria-label", "Show " + item.title);
          dots.append(button);
        });
        window.showHeroSlide(0);
        document.getElementById("heroShowcase").hidden = false;
      }
      await window.loadUpcomingGames();
    } catch (_) {}
  };

  window.loadUpcomingGames = async function () {
    var host = document.getElementById("upcomingGrid");
    if (!host) return;
    try {
      var response = await apiRequest("/api/games?release=upcoming&limit=12&sort=popular");
      host.innerHTML = "";
      (response.results || []).forEach(function (raw) {
        var game = rawgGame(raw);
        var card = text("article", "deal-card");
        var art = text("div", "deal-art");
        if (game.art) { var image = text("img"); image.loading = "lazy"; image.src = game.art; image.alt = game.title + " artwork"; art.append(image); }
        var body = text("div", "deal-body");
        body.append(text("small", "eyebrow", "Upcoming · RAWG"), text("strong", "deal-title", game.title), text("div", "deal-facts", game.releaseLabel + " · " + game.genreLabel));
        var button = text("button", "deal-action", "Explore details ↗"); button.type = "button"; button.dataset.open = game.id; body.append(button);
        card.append(art, body); host.append(card);
      });
      document.getElementById("upcoming").hidden = !(response.results || []).length;
      var allUpcoming = document.getElementById("viewAllUpcoming");
      allUpcoming.textContent = Number(response.total || 0).toLocaleString() + " upcoming games · View all ↗";
    } catch (_) { host.innerHTML = ""; }
  };

  window.loadSteamGenres = async function () {
    try {
      var result = await apiRequest("/api/genres");
      var chipHost = document.getElementById("genreFilters");
      var tileHost = document.querySelector("#genres .genre-grid");
      var chipNames = new Set(Array.from(chipHost.querySelectorAll("[data-genre]")).map(function (node) { return node.dataset.genre.toLowerCase(); }));
      var tileNames = new Set(Array.from(tileHost.querySelectorAll("[data-genre]")).map(function (node) { return node.dataset.genre.toLowerCase(); }));
      (result.results || []).forEach(function (item) {
        var name = String(item.name || "").trim();
        if (!name) return;
        if (!chipNames.has(name.toLowerCase())) {
          var chip = text("button", "chip", name); chip.dataset.genre = name; chip.dataset.remoteGenre = "true"; chipHost.append(chip);
        }
        if (!tileNames.has(name.toLowerCase())) {
          var tile = text("button", "genre-tile steam-genre-tile"); tile.dataset.genre = name; tile.dataset.remoteGenre = "true";
          var glyph = text("span", "genre-glyph", "✦"); var label = text("span"); label.append(text("strong", "", name), text("small", "", Number(item.game_count || 0).toLocaleString() + " RAWG games")); tile.append(glyph, label); tileHost.append(tile);
        }
      });
      var filters = await apiRequest("/api/filters");
      var platform = document.getElementById("platformFilter");
      var params = new URLSearchParams(location.search);
      var prior = platform.value || params.get("platform") || "";
      platform.innerHTML = '<option value="">Any platform</option>';
      (filters.platforms || []).forEach(function (name) { var option = document.createElement("option"); option.value = name; option.textContent = name; platform.append(option); });
      if ((filters.platforms || []).includes(prior)) platform.value = prior;
      var tag = document.getElementById("tagFilter"), selectedTag = tag.value || params.get("tag") || "";
      tag.innerHTML = '<option value="">Any tag</option>';
      (filters.tags || []).forEach(function (name) { var option = document.createElement("option"); option.value = name; option.textContent = name; tag.append(option); });
      if ((filters.tags || []).includes(selectedTag)) tag.value = selectedTag;
    } catch (_) {}
  };

  window.loadRemoteCatalog = async function () {
    if (!remoteCatalogMode) return;
    remoteLoading = true; window.updateCatalogUrl();
    grid.setAttribute("aria-busy", "true");
    grid.innerHTML = Array.from({length:8}, function () { return '<div class="game-skeleton" aria-hidden="true"></div>'; }).join("");
    try {
      var result = await apiRequest("/api/games?" + window.remoteQueryString().toString());
      remoteResults = result.results || []; remoteTotal = Number(result.total || 0); remotePages = Number(result.pages || 0);
      window.rememberSteamGames(remoteResults);
      var state = await apiRequest("/api/catalog/status");
      setRemoteFiltersEnabled(remoteCatalogMode && state.catalog_games > 0);
      if (!state.catalog_games) {
        if (state.rawg_key_configured && state.status !== "failed") {
          document.getElementById("libraryCount").textContent = "Loading RAWG games…";
          grid.innerHTML = '<div class="catalog-setup-note"><strong>Loading games from RAWG…</strong><br>The catalog is being prepared automatically. This page will update as soon as games arrive.</div>';
          sourceNotice("RAWG is loading games automatically. Please wait a moment.", "live");
          pollForCatalog();
        } else {
          document.getElementById("libraryCount").textContent = "RAWG catalog unavailable";
          var message = state.status === "failed" ? (state.last_error || state.message || "RAWG could not complete the request.") : "RAWG_API_KEY is not available to the game server.";
          grid.innerHTML = '<div class="catalog-setup-note"><strong>RAWG games could not be loaded.</strong><br>' + escapeHtml(message) + '</div>';
          sourceNotice(message, "error");
        }
      } else {
        catalogPollAttempts = 0;
        window.renderRemoteGames();
        sourceNotice(remoteTotal.toLocaleString() + " matching RAWG games · Results and filters come from the local database.", "live");
      }
    } catch (error) {
      remoteResults = []; remoteTotal = 0; remotePages = 0;
      grid.innerHTML = '<div class="empty-state"><div><strong>RAWG catalog is not available.</strong>' + escapeHtml(error.message) + '</div></div>';
      sourceNotice("Could not load the RAWG game catalog. Check the game server connection.", "error");
    } finally {
      remoteLoading = false; grid.removeAttribute("aria-busy");
      var pager = document.getElementById("catalogPagination"); pager.hidden = remotePages < 2;
      document.getElementById("catalogPrev").disabled = remotePage <= 1;
      document.getElementById("catalogNext").disabled = remotePage >= remotePages;
      document.getElementById("catalogPageLabel").textContent = remotePages ? "Page " + remotePage + " of " + remotePages : "No catalog pages";
      if (catalogModeButton) catalogModeButton.textContent = "Browse the RAWG catalog ↗";
    }
  };
  window.renderRemoteGames = function () {
    var list = wishlistOnly ? remoteResults.filter(function (raw) { return isSaved(String(raw.app_id)); }) : remoteResults;
    document.getElementById("libraryCount").textContent = remoteTotal.toLocaleString() + (remoteTotal === 1 ? " game" : " RAWG games") + (wishlistOnly ? " · wishlist" : "");
    grid.innerHTML = "";
    if (!list.length) {
      var empty = text("div", "empty-state");
      var copy = text("div");
      copy.append(text("strong", "", wishlistOnly ? "Your saved games are on this page or in your other results." : "No matching RAWG games."));
      copy.append(document.createTextNode(wishlistOnly ? " Open a saved title or clear the wishlist filter." : " Try another title, genre or supported filter."));
      empty.append(copy); grid.append(empty);
    } else {
      list.forEach(function (raw, index) {
        var game = rawgGame(raw), saved = isSaved(game.id);
        var article = text("article", "game-card"); article.style.transitionDelay = String((index % 8) * 35) + "ms";
        var inner = text("div", "game-card-inner"), art = text("div", "card-art");
        art.dataset.open = game.id; art.tabIndex = 0; art.setAttribute("role", "button"); art.setAttribute("aria-label", "View " + game.title + " details");
        if (game.art) { var image = text("img"); image.loading = "lazy"; image.src = game.art; image.alt = game.title + " RAWG artwork"; image.onerror = function () { image.remove(); }; art.append(image); }
        var top = text("div", "card-top"), genre = text("span", "status-pill", game.genreLabel), save = text("button", "save-btn" + (saved ? " saved" : ""), saved ? "♥" : "♡");
        save.dataset.save = game.id; save.setAttribute("aria-label", (saved ? "Remove " : "Add ") + game.title + " " + (saved ? "from" : "to") + " wishlist");
        top.append(genre, save);
        var title = text("div", "art-title"); title.append(text("small", "", game.developer), text("strong", "", game.title)); art.append(top, title);
        var body = text("div", "card-body"), tags = text("div", "tag-row");
        (game.genres.slice(0, 3).length ? game.genres.slice(0, 3) : ["RAWG"]).forEach(function (name) { tags.append(text("span", "genre-tag", name)); });
        var rating = text("div", "rating-row"); rating.append(text("span", "rating", game.review), text("span", "release-mini", game.releaseLabel));
        var info = text("div", "rating-row"); info.append(text("span", "catalog-card-price", "Prices in details"), text("span", "release-mini", game.reviewCount ? game.reviewCount.toLocaleString() + " RAWG ratings" : ""));
        var open = text("button", "card-open"); open.dataset.open = game.id; open.append(text("span", "", "Game details & prices"), text("span", "", "↗"));
        body.append(tags, rating, info, open); inner.append(art, body); article.append(inner); grid.append(article);
      });
    }
    decorateCompareButtons(); renderCompareTray();
    if (cardRevealObserver) grid.querySelectorAll(".game-card").forEach(function (card) { cardRevealObserver.observe(card); });
  };

  function fact(label, value) {
    var node = text("div", "fact"); node.append(text("small", "", label), text("strong", "", value || "Not available")); return node;
  }
  function createMedia(game) {
    var root = text("div", "media-section");
    var heading = text("div", "media-head"), copy = text("div");
    copy.append(text("h3", "", "Game artwork & media"), text("p", "", "Screenshots and trailers supplied by RAWG. Open images in the full-screen gallery."));
    heading.append(copy); root.append(heading);
    var rawTrailers = game.trailers || [];
    if (rawTrailers.length) {
      rawTrailers.slice(0, 2).forEach(function (trailer) {
        var url = trailer.url || trailer.video_url || "";
        if (!validUrl(url)) return;
        var frame = text("div", "steam-trailer");
        if (/youtube\.com\/embed|player\.vimeo\.com\/video/.test(url)) {
          var iframe = text("iframe"); iframe.loading = "lazy"; iframe.src = url; iframe.title = trailer.name || game.title + " trailer";
          iframe.setAttribute("allow", "fullscreen; picture-in-picture"); iframe.referrerPolicy = "strict-origin-when-cross-origin"; frame.append(iframe);
        } else if (/\.mp4(?:\?|$)|\.webm(?:\?|$)/i.test(url)) {
          var video = text("video"); video.controls = true; video.preload = "none";
          if (validUrl(trailer.preview)) video.poster = trailer.preview;
          var source = document.createElement("source"); source.src = url; video.append(source); frame.append(video);
        }
        if (frame.childNodes.length) { frame.append(text("span", "", (trailer.name || "Game trailer") + " · RAWG")); root.append(frame); }
      });
    } else root.append(text("p", "price-state", "No RAWG trailer was returned for this game."));
    var screenshots = (game.screenshots || []).map(function (shot, index) {
      return {url:shot.url || shot.image, preview:shot.thumbnail || shot.preview || shot.url || shot.image, title:"Screenshot " + (index + 1)};
    }).filter(function (shot) { return validUrl(shot.url); });
    gallery = screenshots; galleryIndex = 0;
    if (screenshots.length) {
      var gridEl = text("div", "media-grid");
      screenshots.slice(0, 12).forEach(function (shot, index) {
        var figure = text("figure", "media-shot"), image = text("img"); image.loading = "lazy"; image.src = validUrl(shot.preview) ? shot.preview : shot.url; image.alt = game.title + " " + shot.title; image.className = "rawg-media-image"; image.dataset.lightboxOpen = String(index);
        image.onerror = function () { figure.remove(); };
        figure.append(image, text("figcaption", "", shot.title)); gridEl.append(figure);
      });
      root.append(gridEl);
    } else root.append(text("p", "price-state", "No RAWG screenshots were returned for this game."));
    var links = text("div", "media-cta");
    (game.stores || []).forEach(function (store) {
      if (!validUrl(store.url)) return;
      var anchor = text("a", "btn-small-ghost", (store.store_name || store.storeName || "View store") + " ↗"); anchor.href = store.url; anchor.target = "_blank"; anchor.rel = "noopener noreferrer"; links.append(anchor);
    });
    if (!links.childNodes.length) links.append(text("span", "price-state", "RAWG has not supplied store links for this title."));
    root.append(links, text("p", "media-note", "Media source: RAWG"));
    return root;
  }
  function renderPrices(data, gameId) {
    var host = document.getElementById("priceComparison");
    if (!host) return;
    host.innerHTML = "";
    host.append(text("h3", "", "Where to buy"));
    var state = data.state;
    if (state === "available" || state === "no_offers" || state === "stale" || state === "refreshing") {
      var checked = data.checkedAt ? new Date(data.checkedAt).toLocaleString() : "time unavailable";
      host.append(text("p", "price-state", (data.fresh === false ? "Cached pricing · not live · last checked " : "Price checked ") + checked + " · USD"));
      if (data.currentLowest !== null && data.currentLowest !== undefined) host.append(text("div", "price-lowest", "Lowest current price · $" + Number(data.currentLowest).toFixed(2) + " USD"));
      if (data.cheapestPriceEver !== null && data.cheapestPriceEver !== undefined) {
        var historic = "Lowest recorded price · $" + Number(data.cheapestPriceEver).toFixed(2) + " USD";
        if (data.cheapestPriceEverDate) historic += " · recorded " + new Date(Number(data.cheapestPriceEverDate) * 1000).toLocaleDateString();
        host.append(text("div", "price-lowest", historic));
      }
      var offers = data.offers || [];
      if (offers.length) {
        var scroll = text("div"); scroll.style.overflow = "auto";
        var table = text("table", "price-table"), body = text("tbody");
        offers.forEach(function (offer) {
          var row = text("tr"), storeCell = text("td"), store = text("span", "price-store");
          if (validUrl(offer.storeLogo)) { var logo = text("img"); logo.loading = "lazy"; logo.src = offer.storeLogo; logo.alt = ""; store.append(logo); }
          store.append(document.createTextNode(offer.storeName || "Store"));
          var price = text("td", "price-current", "$" + Number(offer.price).toFixed(2));
          var retail = text("td", "", offer.retailPrice === null || offer.retailPrice === undefined ? "—" : "$" + Number(offer.retailPrice).toFixed(2));
          var discount = text("td", "", Number(offer.savings || 0) > 0 ? Math.round(Number(offer.savings)) + "% OFF" : "—");
          var score = text("td", "", offer.dealRating === null || offer.dealRating === undefined ? "" : Number(offer.dealRating).toFixed(1) + "/10");
          var action = text("td"), link = text("a", "", (String(offer.storeName || "").toLowerCase().includes("steam") ? "View Steam deal" : "View deal") + " ↗");
          if (validUrl(offer.dealURL)) { link.href = offer.dealURL; link.target = "_blank"; link.rel = "noopener noreferrer"; action.append(link); }
          storeCell.append(store); row.append(storeCell, price, retail, discount, score, action); body.append(row);
        });
        table.append(body); scroll.append(table); host.append(scroll);
      } else host.append(text("p", "price-state", "CheapShark returned no current store offers for this exact game match."));
      var refresh = text("button", "btn-small-ghost", "Refresh prices"); refresh.type = "button"; refresh.dataset.priceRefresh = String(gameId); host.append(refresh);
      if (data.error) host.append(text("p", "price-state", data.error));
    } else if (state === "not_available" || state === "ambiguous") {
      host.append(text("p", "price-state", "No unique CheapShark product match was found. Prices are hidden to avoid showing the wrong edition."));
    } else if (state === "loading") host.append(text("p", "price-state", "Loading prices…"));
    else {
      host.append(text("p", "price-state", "Pricing temporarily unavailable" + (data.error ? ": " + data.error : "") + ". Game details remain available."));
      var tryAgain = text("button", "btn-small-ghost", "Try again"); tryAgain.type = "button"; tryAgain.dataset.priceRefresh = String(gameId); host.append(tryAgain);
    }
  }
  function loadPrices(gameId, force) {
    var host = document.getElementById("priceComparison");
    if (host) { host.innerHTML = '<h3>Where to buy</h3><p class="price-state">Loading prices independently from game details…</p><div class="game-skeleton" style="height:75px"></div>'; }
    apiRequest("/api/games/" + encodeURIComponent(gameId) + "/prices" + (force ? "?refresh=1" : ""))
      .then(function (data) { renderPrices(data, gameId); })
      .catch(function (error) { renderPrices({state:"unavailable",error:error.message}, gameId); });
  }
  function openRawgGame(raw) {
    var game = rawgGame(raw);
    selectedGame = game;
    document.getElementById("modalTitle").textContent = game.title;
    document.getElementById("modalGenre").textContent = game.genreLabel;
    document.getElementById("modalMeta").textContent = "";
    [game.releaseLabel, game.developer, game.platform].forEach(function (value) { document.getElementById("modalMeta").append(text("span", "", value)); });
    var hero = document.getElementById("modalHero");
    hero.style.backgroundImage = game.art ? 'linear-gradient(0deg,rgba(11,12,18,.8),rgba(11,12,18,.13)),url("' + game.art.replace(/["\\]/g, "") + '")' : "linear-gradient(125deg,#1e1d2a,#10141a)";
    var overview = document.getElementById("panel-overview");
    overview.innerHTML = '<div class="overview-grid"><div><h3>About this game</h3><div class="overview-copy" data-about></div><div class="rawg-facts fact-grid" data-facts></div><div class="rawg-facts fact-grid" data-extra></div><div class="modal-actions" data-actions></div><div class="price-comparison" id="priceComparison"></div></div><aside class="verdict-box"><small>Data source</small><p>Game facts, player ratings and artwork below are supplied by RAWG. GAMEVAULT has not added an editorial review for this title.</p><button class="btn-small-ghost" data-save="SAVE_ID">♡ Add to wishlist</button><p class="media-note">Source and artwork attribution: RAWG</p></aside></div>'.replace("SAVE_ID", game.id);
    overview.querySelector("[data-about]").textContent = game.description;
    var facts = overview.querySelector("[data-facts]");
    facts.append(fact("Release date", game.releaseLabel), fact("Developer", game.developer), fact("Publisher", game.publisher), fact("Platforms", game.platform));
    var extra = overview.querySelector("[data-extra]");
    extra.append(fact("RAWG rating", game.rating === null || Number.isNaN(game.rating) ? "Not available" : game.rating.toFixed(1) + " / 5"), fact("Metacritic", game.metacritic === null || game.metacritic === undefined ? "Not available" : game.metacritic), fact("Player ratings", game.ratingsCount ? game.ratingsCount.toLocaleString() : "Not available"), fact("Average playtime", raw.playtime ? raw.playtime + " hours" : "Not available"));
    var actions = overview.querySelector("[data-actions]");
    if (game.steam) { var steam = text("a", "btn btn-primary", "See on Steam ↗"); steam.href = game.steam; steam.target = "_blank"; steam.rel = "noopener noreferrer"; actions.append(steam); }
    (game.stores || []).forEach(function (store) {
      if (!validUrl(store.url)) return;
      var link = text("a", "btn-small-ghost", (store.store_name || store.storeName || "View store") + " ↗"); link.href = store.url; link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link);
    });
    var deals = text("a", "btn-small-ghost", "Browse deals ↗"); deals.href = "/deals?title=" + encodeURIComponent(game.title); actions.append(deals);
    var compare = text("button", "btn-small-ghost", "+ Compare"); compare.dataset.compare = game.id; actions.append(compare);
    var save = overview.querySelector("[data-save]"); save.innerHTML = isSaved(game.id) ? "♥ Saved to wishlist" : "♡ Add to wishlist";

    document.getElementById("panel-media").innerHTML = "";
    document.getElementById("panel-media").append(createMedia(game));
    var reviews = document.getElementById("panel-reviews");
    reviews.innerHTML = '<div class="steam-review"><span>✦</span> RAWG rating</div><h3>Player rating snapshot</h3><p class="overview-copy" data-rating></p><p class="media-note">These are aggregate ratings, not written player reviews. Steam rating data is only displayed when CheapShark supplies it.</p>';
    reviews.querySelector("[data-rating]").textContent = game.rating === null || Number.isNaN(game.rating)
      ? "RAWG has not supplied a rating for this title."
      : "RAWG rates this game " + game.rating.toFixed(1) + " out of 5" + (game.ratingsCount ? " across " + game.ratingsCount.toLocaleString() + " player ratings." : ".");
    if (game.metacritic !== null && game.metacritic !== undefined) reviews.append(fact("Metacritic", game.metacritic + " / 100"));

    var specs = document.getElementById("panel-specs");
    specs.innerHTML = '<h3>System requirements</h3><p class="overview-copy">Only requirements supplied by RAWG are shown.</p>';
    if (game.min || game.rec) {
      specs.insertAdjacentHTML("beforeend", '<div class="req-toggle"><button class="active" data-tier="min">Minimum</button><button data-tier="rec">Recommended</button></div><div class="spec-list" id="specList"></div>');
      window.renderSpecs();
    } else {
      specs.append(text("div", "verdict-box", "RAWG has not supplied system requirements for this title."));
    }
    document.querySelectorAll(".modal-tab").forEach(function (tab) { tab.classList.toggle("active", tab.dataset.tab === "overview"); });
    document.querySelectorAll(".tab-panel").forEach(function (panel) { panel.classList.toggle("active", panel.id === "panel-overview"); });
    openBackdrop("gameBackdrop");
    loadPrices(game.id, false);
    if (rawgKeyConfigured && raw.detail_status !== "complete" && !document.body.dataset.rawgDetailsLoading) {
      document.body.dataset.rawgDetailsLoading = "true";
      apiRequest("/api/games/" + encodeURIComponent(game.id)).then(function (full) {
        var index = remoteResults.findIndex(function (item) { return Number(item.app_id) === Number(full.app_id); });
        if (index >= 0) remoteResults[index] = full;
        var shelfIndex = steamShelfRecords.findIndex(function (item) { return Number(item.app_id) === Number(full.app_id); });
        if (shelfIndex >= 0) steamShelfRecords[shelfIndex] = full;
        if (selectedGame && Number(selectedGame.app) === Number(full.app_id)) openRawgGame(full);
      }).catch(function () {}).finally(function () { delete document.body.dataset.rawgDetailsLoading; });
    }
  }
  window.openGame = function (id) {
    var rows = (remoteResults || []).concat(steamShelfRecords || []);
    var raw = rows.find(function (item) { return String(item.app_id) === String(id); });
    if (!raw) {
      try { raw = JSON.parse(localStorage.getItem("playscape-steam-cache") || "[]").find(function (item) { return String(item.app_id) === String(id); }); } catch (_) {}
    }
    if (raw && raw.rawg_id) return openRawgGame(raw);
    return previousOpenGame ? previousOpenGame(id) : undefined;
  };
  window.renderSpecs = function () {
    var host = document.getElementById("specList");
    if (!host || !selectedGame) return;
    host.innerHTML = "";
    var requirement = selectedGame[requirementTier];
    if (!requirement) {
      host.append(text("div", "spec-item", requirementTier === "rec" ? "RAWG did not supply a recommended tier." : "RAWG did not supply a minimum tier."));
    } else if (typeof requirement === "string") {
      var item = text("div", "spec-item"); item.append(text("small", "", requirementTier === "rec" ? "Recommended · RAWG" : "Minimum · RAWG"), text("span", "", requirement.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim())); host.append(item);
    } else {
      Object.keys(requirement).forEach(function (key) {
        var row = text("div", "spec-item"); row.append(text("small", "", key), text("span", "", String(requirement[key]).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim())); host.append(row);
      });
    }
    document.querySelectorAll(".req-toggle button").forEach(function (button) { button.classList.toggle("active", button.dataset.tier === requirementTier); });
  };
  window.showHeroSlide = function (index) {
    if (!featuredHeroes || !featuredHeroes.length) return;
    heroIndex = (index + featuredHeroes.length) % featuredHeroes.length;
    var item = featuredHeroes[heroIndex];
    var cover = document.getElementById("heroCover"), background = document.getElementById("heroBg");
    var title = document.getElementById("heroTitleGame"), kicker = document.getElementById("heroKicker");
    var rating = document.getElementById("heroRating"), style = document.getElementById("heroStyle");
    [title,kicker,rating,style].forEach(function (node) { node.style.opacity = "0"; });
    clearTimeout(heroTextTimer);
    heroTextTimer = setTimeout(function () {
      title.textContent = item.title; kicker.textContent = "RAWG pick · " + item.genre; rating.textContent = item.review; style.textContent = item.style;
      cover.alt = item.title + " game art"; [title,kicker,rating,style].forEach(function (node) { node.style.opacity = "1"; });
    }, 180);
    cover.style.opacity = "0.15";
    cover.onload = function () { cover.style.opacity = "1"; };
    cover.onerror = function () { cover.removeAttribute("src"); cover.style.opacity = "0"; };
    if (item.art) cover.src = item.art; else cover.removeAttribute("src");
    var bg = item.background || item.art;
    background.style.backgroundImage = bg && validUrl(bg) ? 'linear-gradient(90deg,#090b10 0%,rgba(9,11,16,.96) 23%,rgba(9,11,16,.64) 56%,rgba(9,11,16,.45) 100%),linear-gradient(0deg,#090b10 0%,transparent 38%,rgba(9,11,16,.1) 100%),url("' + bg.replace(/["\\]/g, "") + '")' : "linear-gradient(120deg,#10131a,#171420)";
    document.querySelectorAll(".hero-dot").forEach(function (dot, dotIndex) { dot.classList.toggle("active", dotIndex === heroIndex); dot.setAttribute("aria-current", dotIndex === heroIndex ? "true" : "false"); });
    document.getElementById("heroCount").innerHTML = String(heroIndex + 1).padStart(2, "0") + " <span>/ " + String(featuredHeroes.length).padStart(2, "0") + "</span>";
    document.getElementById("heroTrailer").setAttribute("aria-label", "See " + item.title + " featured details");
  };
  window.loadDeals = async function () {
    if (!routeDeals) return;
    var host = document.getElementById("dealsGrid"), status = document.getElementById("dealsStatus");
    host.setAttribute("aria-busy", "true");
    host.innerHTML = Array.from({length:6}, function () { return '<div class="game-skeleton"></div>'; }).join("");
    status.textContent = "Loading current CheapShark offers…";
    var params = new URLSearchParams({page:String(dealsPage),pageSize:"24",sortBy:document.getElementById("dealSort").value,desc:"1"});
    var title = document.getElementById("dealTitle").value.trim();
    var store = document.getElementById("dealStore").value, price = document.getElementById("dealPriceMax").value;
    var meta = document.getElementById("dealMetacritic").value, rating = document.getElementById("dealSteamRating").value;
    var count = document.getElementById("dealReviewCount").value;
    if (title) params.set("title", title);
    if (store) params.set("storeID", store);
    if (price) params.set("upperPrice", String(Math.min(500, Math.max(0, Number(price)))));
    if (meta) params.set("metacritic", String(Math.min(100, Number(meta))));
    if (rating) params.set("steamRating", String(Math.min(100, Number(rating))));
    if (count) params.set("minimumReviewCount", String(Math.max(0, Number(count))));
    if (document.getElementById("dealOnSale").checked) params.set("onSale", "1");
    try {
      var data = await apiRequest("/api/deals?" + params.toString());
      dealsPages = Number(data.pages || 0);
      host.innerHTML = "";
      (data.results || []).forEach(function (deal) {
        var current = Number(deal.price), retail = Number(deal.retailPrice), discount = Number(deal.savings);
        if (!Number.isFinite(current) || !Number.isFinite(retail)) return;
        var card = text("article", "deal-card"), art = text("div", "deal-art"), imageUrl = deal.thumb || (deal.playscapeGame && deal.playscapeGame.background_image);
        if (validUrl(imageUrl)) { var img = text("img"); img.loading = "lazy"; img.src = imageUrl; img.alt = (deal.gameTitle || deal.title || "Game") + " artwork"; img.onerror = function () { img.remove(); }; art.append(img); }
        if (Number.isFinite(discount) && discount > 0) art.append(text("span", "deal-discount", Math.round(discount) + "% OFF"));
        var body = text("div", "deal-body"), storeLine = text("div", "deal-store");
        if (validUrl(deal.storeLogo)) { var logo = text("img"); logo.loading = "lazy"; logo.src = deal.storeLogo; logo.alt = ""; storeLine.append(logo); }
        storeLine.append(document.createTextNode(deal.storeName || "Store"));
        body.append(storeLine, text("strong", "deal-title", deal.gameTitle || deal.title || "Untitled game"));
        var prices = text("div", "deal-prices"); prices.append(text("span", "deal-price", "$" + current.toFixed(2)));
        if (retail > current) prices.append(text("span", "deal-retail", "$" + retail.toFixed(2)));
        body.append(prices);
        var details = [];
        if (deal.dealRating) details.push("Deal rating " + Number(deal.dealRating).toFixed(1) + "/10");
        if (Number(deal.metacriticScore) > 0) details.push("Metacritic " + deal.metacriticScore);
        if (deal.steamRatingText) details.push("Steam " + deal.steamRatingText);
        if (deal.steamRatingCount) details.push(Number(deal.steamRatingCount).toLocaleString() + " Steam reviews");
        if (Number(deal.releaseDate) > 0) details.push(new Date(Number(deal.releaseDate) * 1000).toLocaleDateString(undefined, {year:"numeric",month:"short",day:"numeric"}));
        body.append(text("div", "deal-facts", details.join(" · ") || "Current offer · USD"));
        var action = text("a", "deal-action", "View deal ↗");
        if (validUrl(deal.dealURL)) { action.href = deal.dealURL; action.target = "_blank"; action.rel = "noopener noreferrer"; }
        body.append(action); card.append(art, body); host.append(card);
      });
      if (!host.childNodes.length) host.append(text("div", "empty-state", data.state === "unavailable" ? "Pricing temporarily unavailable." : "No deals match these filters."));
      status.textContent = (data.cached ? "Cached " : "Live ") + "CheapShark offers · USD" + (data.state === "stale" ? " · showing stale cached prices" : "");
    } catch (error) {
      host.innerHTML = ""; host.append(text("div", "empty-state", "Pricing temporarily unavailable: " + error.message)); status.textContent = "CheapShark could not load this page."; dealsPages = 0;
    } finally {
      host.removeAttribute("aria-busy");
      document.getElementById("dealsPagination").hidden = dealsPages < 2;
      document.getElementById("dealsPrev").disabled = dealsPage <= 1;
      document.getElementById("dealsNext").disabled = dealsPage >= dealsPages;
      document.getElementById("dealsPageLabel").textContent = dealsPages ? "Page " + dealsPage + " of " + dealsPages : "No deal pages";
    }
  };
  async function populateDealStores() {
    try {
      var data = await apiRequest("/api/stores"), select = document.getElementById("dealStore"), current = select.value;
      select.innerHTML = '<option value="">All stores</option>';
      (data.results || []).filter(function (store) { return store.isActive !== 0; }).forEach(function (store) {
        var option = document.createElement("option"); option.value = store.storeID; option.textContent = store.name; select.append(option);
      });
      select.value = current;
    } catch (_) {}
  }
  function scheduleDeals() { clearTimeout(dealTimer); dealTimer = setTimeout(function () { dealsPage = 1; window.loadDeals(); }, 300); }
  ["dealTitle","dealStore","dealPriceMax","dealMetacritic","dealSteamRating","dealReviewCount","dealSort","dealOnSale"].forEach(function (id) {
    var node = document.getElementById(id);
    node.addEventListener("change", scheduleDeals);
    if (node.type === "number" || node.type === "search") node.addEventListener("input", function () { clearTimeout(dealTimer); dealTimer = setTimeout(function () { dealsPage = 1; window.loadDeals(); }, 450); });
  });
  document.getElementById("dealFilterReset").addEventListener("click", function () {
    ["dealTitle","dealStore","dealPriceMax","dealMetacritic","dealSteamRating","dealReviewCount"].forEach(function (id) { document.getElementById(id).value = ""; });
    document.getElementById("dealSort").value = "DealRating"; document.getElementById("dealOnSale").checked = true; scheduleDeals();
  });
  document.getElementById("dealsPrev").addEventListener("click", function () { if (dealsPage > 1) { dealsPage -= 1; window.loadDeals(); } });
  document.getElementById("dealsNext").addEventListener("click", function () { if (dealsPage < dealsPages) { dealsPage += 1; window.loadDeals(); } });
  document.addEventListener("click", function (event) {
    var refresh = event.target.closest("[data-price-refresh]");
    if (refresh) {
      var host = document.getElementById("priceComparison");
      if (host) { host.innerHTML = '<h3>Where to buy</h3><p class="price-state">Refreshing prices…</p>'; }
      apiRequest("/api/games/" + encodeURIComponent(refresh.dataset.priceRefresh) + "/prices?refresh=1")
        .then(function (data) { renderPrices(data, refresh.dataset.priceRefresh); })
        .catch(function (error) { renderPrices({state:"unavailable",error:error.message}, refresh.dataset.priceRefresh); });
      return;
    }
    var image = event.target.closest("[data-lightbox-open]");
    if (image) { galleryIndex = Number(image.dataset.lightboxOpen); showGallery(); return; }
    var step = event.target.closest("[data-lightbox-step]");
    if (step) { galleryIndex = (galleryIndex + Number(step.dataset.lightboxStep) + gallery.length) % gallery.length; showGallery(); return; }
    if (event.target.closest("[data-lightbox-close]") || event.target.id === "mediaLightbox") hideGallery();
  });
  function showGallery() {
    if (!gallery.length) return;
    var item = gallery[galleryIndex];
    document.getElementById("lightboxImage").src = item.url;
    document.getElementById("lightboxImage").alt = item.title;
    document.getElementById("lightboxCaption").textContent = item.title + " · " + (galleryIndex + 1) + " / " + gallery.length;
    var lightbox = document.getElementById("mediaLightbox"); lightbox.classList.add("open"); lightbox.setAttribute("aria-hidden", "false");
  }
  function hideGallery() {
    var lightbox = document.getElementById("mediaLightbox"); lightbox.classList.remove("open"); lightbox.setAttribute("aria-hidden", "true");
    document.getElementById("lightboxImage").removeAttribute("src");
  }
  document.addEventListener("keydown", function (event) {
    if (!document.getElementById("mediaLightbox").classList.contains("open")) return;
    if (event.key === "Escape") hideGallery();
    if (event.key === "ArrowRight" && gallery.length) { galleryIndex = (galleryIndex + 1) % gallery.length; showGallery(); }
    if (event.key === "ArrowLeft" && gallery.length) { galleryIndex = (galleryIndex - 1 + gallery.length) % gallery.length; showGallery(); }
  });

  if (routeDeals) {
    document.body.classList.add("deals-route");
    document.getElementById("dealTitle").value = new URLSearchParams(location.search).get("title") || "";
    populateDealStores().finally(function () { window.loadDeals(); });
  } else {
    if (routeGames) {
      remoteCatalogMode = true;
      document.body.classList.add("games-route");
    }
    catalogModeButton && catalogModeButton.addEventListener("click", function (event) {
      event.preventDefault(); event.stopImmediatePropagation();
      if (location.pathname.replace(/\/$/, "") === "/games") window.loadRemoteCatalog();
      else location.href = "/games";
    }, true);
    window.refreshCatalogStatus().then(function (state) {
      document.documentElement.classList.remove("playscape-api-pending");
      if (remoteCatalogMode && state) window.loadRemoteCatalog();
    });
  }
})();
