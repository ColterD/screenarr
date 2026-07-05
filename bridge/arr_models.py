from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SystemStatus(BaseModel):
    appName: str = "Screenarr"
    version: str = "0.1.0"
    instanceName: str = "screenarr"


class QualityProfile(BaseModel):
    id: int
    name: str


class RootFolderResponse(BaseModel):
    id: int
    path: str
    freeSpace: int = 0


class TagResponse(BaseModel):
    id: int
    label: str


class MovieLookup(BaseModel):
    title: str
    originalTitle: str | None = None
    tmdbId: int
    year: int = 0
    titleSlug: str
    overview: str | None = None
    images: list[dict[str, Any]] = Field(default_factory=list)


class AddMovieOptions(BaseModel):
    searchForMovie: bool = True


class AddMovieRequest(BaseModel):
    title: str
    originalTitle: str | None = None
    tmdbId: int
    year: int = 0
    titleSlug: str | None = None
    qualityProfileId: int | None = None
    rootFolderPath: str | None = None
    monitored: bool = True
    minimumAvailability: str | None = None
    tags: list[int] = Field(default_factory=list)
    addOptions: AddMovieOptions = Field(default_factory=AddMovieOptions)


class AddMovieResponse(BaseModel):
    id: int
    title: str


class SeriesSeason(BaseModel):
    seasonNumber: int
    monitored: bool = True


class SeriesLookup(BaseModel):
    title: str
    sortTitle: str | None = None
    tvdbId: int
    tmdbId: int | None = None
    year: int = 0
    titleSlug: str
    overview: str | None = None
    seriesType: str | None = None
    images: list[dict[str, Any]] = Field(default_factory=list)
    seasons: list[SeriesSeason] = Field(default_factory=list)
    status: str | None = None


class AddSeriesOptions(BaseModel):
    searchForMissingEpisodes: bool = True
    searchForCutoffUnmetEpisodes: bool = False
    monitor: str | None = "all"


class AddSeriesRequest(BaseModel):
    title: str
    tvdbId: int
    year: int = 0
    titleSlug: str | None = None
    seasons: list[SeriesSeason] = Field(default_factory=list)
    qualityProfileId: int | None = None
    languageProfileId: int | None = None
    rootFolderPath: str | None = None
    monitored: bool = True
    seasonFolder: bool = True
    seriesType: str | None = None
    tags: list[int] = Field(default_factory=list)
    addOptions: AddSeriesOptions = Field(default_factory=AddSeriesOptions)


class AddSeriesResponse(BaseModel):
    id: int
    title: str
