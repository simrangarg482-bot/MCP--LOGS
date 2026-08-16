import type { ScoredChunk } from "@/types/ask";
import { searchSimilarIncidents } from "./ask";

/**
 * The real backend has no generic `GET /search` endpoint at all -- the
 * previous `globalSearch()` called one that doesn't exist, with the wrong
 * HTTP method (GET with a `?q=` param; every real search route is POST
 * with a JSON body) and an invented response shape (`SearchResult` with a
 * `type: "incident"|"knowledge"|"slack"|"github"` taxonomy that doesn't
 * exist on any real response).
 *
 * The closest real, general-purpose search is `POST /search/similar-
 * incidents` (`agents.service.search_similar_incidents`) -- despite its
 * name, it searches every retrieval collection (no `collection` filter),
 * unlike `search_recent_changes`, which defaults to just the "code"
 * collection. Real results are `ScoredChunk`s grouped by `collection`
 * (documentation/code/conversations), not the fictional type taxonomy.
 */
export async function globalSearch(query: string): Promise<ScoredChunk[]> {
  if (!query.trim()) return [];
  return searchSimilarIncidents(query, 20);
}
