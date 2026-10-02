---
title: Geo Assistant
emoji: 🗺️
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Ask questions about Luxembourg open geodata
---

# Geo Assistant

Ask questions about open geospatial data for Luxembourg in plain language, for example:

- Which schools are within 300 m of a public transport stop?
- How many stops are there in each commune?
- Which canton has the most schools?
- What are the 3 nearest stops to longitude 6.13, latitude 49.61?

An LLM chooses among a small set of whitelisted spatial tools and never writes code: every number comes from GeoPandas, and each answer shows the tool calls that produced it and the result on a map.

Source code, architecture and tests: <https://github.com/rabieessayeh/geo-assistant>

## About this demo

- It runs on a free LLM quota, so questions are limited per visitor and per hour. The spatial tools stay available without limit through `POST /tools` (see `/docs`).
- Answers come from the data below, with its limits: the school list dates from 2021 and only schools whose address could be geocoded to the house number are included.

## Data and attribution

The layers were downloaded from [data.public.lu](https://data.public.lu) with `scripts/fetch_lux_data.py`. The exact resources and the download date are recorded in `data/lux/SOURCES.json`.

| Layer | Dataset | Publisher | Licence |
| --- | --- | --- | --- |
| `communes`, `cantons` | [Limites administratives du Grand-Duché de Luxembourg](https://data.public.lu/en/datasets/limites-administratives-du-grand-duche-de-luxembourg/) | Administration du cadastre et de la topographie | CC0 |
| `stops` | [Horaires et arrêts des transport publics (GTFS)](https://data.public.lu/en/datasets/horaires-et-arrets-des-transport-publics-gtfs/) | Administration des transports publics | CC BY |
| `schools` | [Adresses des bâtiments scolaires 2021](https://data.public.lu/en/datasets/adresses-des-batiments-scolaires-2021/) | Ministère de l'Éducation nationale, de l'Enfance et de la Jeunesse | CC0 |

The layers were modified: columns were reduced and renamed, stops were extracted from the GTFS feed, and school addresses were geocoded with the national geocoder (geoportail.lu). Map background © OpenStreetMap contributors.

## Configuration

The LLM API key is read from the Space secret `LLM_API_KEY`. The endpoint, model and rate limits are set in the `Dockerfile` and can be overridden with Space variables.

Author: Rabie ES-SAYEH · code under the MIT licence.
