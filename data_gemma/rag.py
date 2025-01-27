# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""RAG Flow."""

import logging
import time

from dev.data_gemma import base
from dev.data_gemma import datacommons
from dev.data_gemma import prompts
from dev.data_gemma import validate

_MAX_QUESTIONS = 25


class RAGFlow(base.Flow):
  """Retrieval Augmented Generation."""

  def __init__(
      self,
      llm_question: base.LLM,
      llm_answer: base.LLM,
      data_fetcher: datacommons.DataCommons,
      verbose: bool = True,
      in_context: bool = False,
      validate_dc_responses: bool = False,
      metrics_list: str = '',
  ):
    self.llm_question = llm_question
    self.llm_answer = llm_answer
    self.data_fetcher = data_fetcher
    self.options = base.Options(verbose=verbose)
    self.in_context = in_context
    self.validate_dc_responses = validate_dc_responses
    self.metrics_list = metrics_list

  def query(
      self,
      query: str,
  ) -> base.FlowResponse:

    #
    # First call FT or V LLM model to get questions for Retrieval
    #
    if self.in_context:
      if self.metrics_list:
        prompt = prompts.RAG_IN_CONTEXT_PROMPT_WITH_VARS
        self.options.vlog(
            '... [RAG] Calling UNTUNED model for DC '
            'questions with all DC vars in prompt'
        )
        ques_resp = self.llm_question.query(
            prompt.format(metrics_list=self.metrics_list, sentence=query)
        )
      else:
        prompt = prompts.RAG_IN_CONTEXT_PROMPT
        self.options.vlog('... [RAG] Calling UNTUNED model for DC questions')
        ques_resp = self.llm_question.query(prompt.format(sentence=query))
    else:
      prompt = prompts.RAG_FINE_TUNED_PROMPT
      self.options.vlog('... [RAG] Calling FINETUNED model for DC questions')
      ques_resp = self.llm_question.query(prompt.format(sentence=query))
    llm_calls = [ques_resp]
    if not ques_resp.response:
      return base.FlowResponse(llm_calls=llm_calls)

    questions = [q.strip() for q in ques_resp.response.split('\n') if q.strip()]
    questions = list(set(questions))[:_MAX_QUESTIONS]

    self.options.vlog('... [RAG] Making DC Calls')
    start = time.time()
    try:
      q2resp = self.data_fetcher.calln(questions, self.data_fetcher.table)
    except Exception as e:
      logging.warning(e)
      q2resp = {}
      pass
    dc_duration = time.time() - start

    if self.validate_dc_responses:
      q2resp = validate.run_validation(
          q2resp, self.llm_answer, self.options, llm_calls
      )

    table_parts: list[str] = []
    table_titles = set()
    dc_calls = []
    for resp in q2resp.values():
      tidx = len(dc_calls) + 1
      if resp.table and resp.title not in table_titles:
        table_parts.append(f'Table {tidx}: {resp.answer()}')
        table_titles.add(resp.title)
      resp.id = tidx
      dc_calls.append(resp)
    if table_parts:
      prompt = prompts.RAG_FINAL_ANSWER_PROMPT
      tables_str = '\n'.join(table_parts)
      final_prompt = prompt.format(sentence=query, table_str=tables_str)
    else:
      self.options.vlog('... [RAG] No stats found!')
      final_prompt = query
      tables_str = ''

    self.options.vlog('... [RAG] Calling UNTUNED model for final response')
    ans_resp = self.llm_answer.query(final_prompt)
    llm_calls.append(ans_resp)

    if '[NO ANSWER]' in ans_resp.response:
      self.options.vlog('... [RAG] Retrying original query!')
      ans_resp = self.llm_answer.query(query)
      llm_calls.append(ans_resp)

    if not ans_resp.response:
      return base.FlowResponse(
          llm_calls=llm_calls, dc_duration_secs=dc_duration
      )

    return base.FlowResponse(
        main_text=ans_resp.response,
        tables_str=tables_str,
        llm_calls=llm_calls,
        dc_duration_secs=dc_duration,
        dc_calls=dc_calls,
    )
