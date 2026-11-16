# Third-party sources

## TheAgentCompany

Benchmark tasks, task images, ownCloud seed data, initialization and evaluation are supplied by [TheAgentCompany](https://github.com/TheAgentCompany/TheAgentCompany), pinned to source commit `98b68ef82a47690c316f42fddb05baafaab56851` and image version `1.0.0`. `infra/compose.yml` adapts the upstream ownCloud service configuration; the normalized-score formula follows upstream `evaluation/summarise_results.py`.

The upstream project is distributed under the following MIT notice. Its source, images and task data are downloaded separately and are not included in this Git repository.

> MIT License
>
> Copyright (c) 2024 TheAgentCompany
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## OpenCode

OpenCode is downloaded from the [official v2.2.7 release](https://github.com/anomalyco/opencode/releases/tag/v2.2.7). Release archives and executables remain in the ignored `.tools/` directory and are not redistributed here. See the [upstream repository](https://github.com/anomalyco/opencode/tree/v2.2.7) for its applicable notices.

## Agent Runtime SDK

The SDK is a separately supplied dependency. Its source and third-party notices remain with the user's SDK checkout and ignored local snapshot. This repository does not redistribute that SDK, Duet product code, credentials, private configuration, or runtime data.

## Model pricing

Token estimates use the dated rates in `scripts/pricing.py`, sourced from official OpenAI model and prompt-caching documentation. They are estimates, not account invoices. Access to the model and its billing are supplied by the user.
