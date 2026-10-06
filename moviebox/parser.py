from moviebox.client import MovieBoxClient
from moviebox.models import (
    DownloadableFilesModel,
    SearchResultsModel,
    SubjectModel,
)


class MovieBoxParser:
    def __init__(self, client: MovieBoxClient):
        self.client = client

    async def search(self, query: str, is_movie: bool, page: int = 1, per_page: int = 10) -> SearchResultsModel:
        subject_type = 1 if is_movie else 2
        payload = {
            "keyword": query,
            "page": page,
            "perPage": per_page,
            "subjectType": subject_type,
            "tabId": "All"
        }
        
        try:
            data = await self.client.post("/wefeed-mobile-bff/subject-api/search/v2", payload)
            
            results = data.get("results", [])
            items = []
            if results:
                subjects = results[0].get("subjects", [])
                for sub in subjects:
                    if sub.get("subjectType") == subject_type:
                        items.append(sub)
            
            data["items"] = items
            return SearchResultsModel.model_validate(data)
        except Exception:
            payload.pop("tabId", None)
            data = await self.client.post("/wefeed-mobile-bff/subject-api/search", payload)
            
            items = []
            for sub in data.get("items", []):
                if sub.get("subjectType") == subject_type:
                    items.append(sub)
            
            data["items"] = items
            return SearchResultsModel.model_validate(data)

    async def get_download_links(self, subject_id: str, resolution: str, is_movie: bool, season: int = 1, episode: int = 1, page: int = 1, per_page: int = 20) -> DownloadableFilesModel:
        path = f"/wefeed-mobile-bff/subject-api/resource?subjectId={subject_id}&resolution={resolution}&page={page}&perPage={per_page}"
        
        if not is_movie:
            path += f"&se={season}&epFrom={episode}&epTo={episode}&all=0&pagerMode=0"
            
        data = await self.client.get(path)
        return DownloadableFilesModel.model_validate(data)

    async def get_play_info(self, subject_id: str, season: int = 0, episode: int = 0) -> dict:
        if season == 0 and episode == 0:
            path = f"/wefeed-mobile-bff/subject-api/play-info/v2?subjectId={subject_id}"
        else:
            path = f"/wefeed-mobile-bff/subject-api/play-info/v2?subjectId={subject_id}&se={season}&ep={episode}"
        try:
            return await self.client.get(path)
        except Exception:
            fallback = f"/wefeed-mobile-bff/subject-api/play-info?subjectId={subject_id}&se={season}&ep={episode}"
            return await self.client.get(fallback)

    async def get_subtitles(self, subject_id: str, stream_id: str = "") -> list[dict]:
        endpoints = []
        if stream_id:
            endpoints.append(f"/wefeed-mobile-bff/subject-api/get-stream-captions?subjectId={subject_id}&streamId={stream_id}")
            endpoints.append(f"/wefeed-mobile-bff/subject-api/get-ext-captions?subjectId={subject_id}&resourceId={stream_id}&episode=0")
        else:
            endpoints.append(f"/wefeed-mobile-bff/subject-api/get-ext-captions?subjectId={subject_id}&resourceId=&episode=0")

        subtitles = []
        seen_urls = set()
        for ep in endpoints:
            try:
                res = await self.client.get(ep)
                captions = res.get("extCaptions") or res.get("captions") or []
                for cap in captions:
                    url = cap.get("url")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        subtitles.append({
                            "id": str(cap.get("id", len(subtitles))),
                            "url": url,
                            "lang": cap.get("lan", "en"),
                            "name": cap.get("lanName", cap.get("lan", "English"))
                        })
            except Exception:
                pass
        return subtitles

