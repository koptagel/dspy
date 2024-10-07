from concurrent.futures import ThreadPoolExecutor
import functools
import os
from pathlib import Path
import re
from typing import Any, Dict, List
import ujson


from dspy import logger
from dspy.clients.finetune import FinetuneJob
from dspy.clients.self_hosted import is_self_hosted_model
from dspy.clients.openai import is_openai_model, finetune_openai, FinetuneJobOpenAI
from dspy.clients.anyscale import is_anyscale_model, finetune_anyscale, FinetuneJobAnyScale


try:
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning)
        if "LITELLM_LOCAL_MODEL_COST_MAP" not in os.environ:
             os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        import litellm  
        litellm.telemetry = False

    from litellm.caching import Cache
    disk_cache_dir = os.environ.get('DSPY_CACHEDIR') or os.path.join(Path.home(), '.dspy_cache')
    litellm.cache = Cache(disk_cache_dir=disk_cache_dir, type="disk")

except ImportError:
    class LitellmPlaceholder:
        def __getattr__(self, _): raise ImportError("The LiteLLM package is not installed. Run `pip install litellm`.")

    litellm = LitellmPlaceholder()


#-------------------------------------------------------------------------------
#    LiteLLM Client
#-------------------------------------------------------------------------------
class LM:
    def __init__(self, model, model_type='chat', temperature=0.0, max_tokens=1000, cache=True, **kwargs):
        self.model = model
        self.model_type = model_type
        self.cache = cache
        self.kwargs = dict(temperature=temperature, max_tokens=max_tokens, **kwargs)
        self.history = []

        if "o1-" in model:  # TODO: This is error prone!
            assert max_tokens >= 5000 and temperature == 1.0, \
                "OpenAI's o1-* models require passing temperature=1.0 and max_tokens >= 5000 to `dspy.LM(...)`"
                
    
    def __call__(self, prompt=None, messages=None, **kwargs):
        # Build the request.
        cache = kwargs.pop("cache", self.cache)
        messages = messages or [{"role": "user", "content": prompt}]
        kwargs = {**self.kwargs, **kwargs}

        # Make the request and handle LRU & disk caching.
        if self.model_type == "chat": completion = cached_litellm_completion if cache else litellm_completion
        else: completion = cached_litellm_text_completion if cache else litellm_text_completion

        response = completion(ujson.dumps(dict(model=self.model, messages=messages, **kwargs)))
        outputs = [c.message.content if hasattr(c, "message") else c["text"] for c in response["choices"]]

        # Logging, with removed api key & where `cost` is None on cache hit.
        kwargs = {k: v for k, v in kwargs.items() if not k.startswith("api_")}
        entry = dict(prompt=prompt, messages=messages, kwargs=kwargs, response=response)
        entry = dict(**entry, outputs=outputs, usage=dict(response["usage"]))
        entry = dict(**entry, cost=response.get("_hidden_params", {}).get("response_cost"))
        self.history.append(entry)

        return outputs
    
    def inspect_history(self, n: int = 1):
        _inspect_history(self, n)

    def launch(self):
        """Send a request to the provider to launch the model, if needed."""
        if is_self_hosted_model(self.model):
            self_hosted_model_launch(self)
        logger.debug(f"`LM.launch()` is called for the auto-launched model {self.model} -- no action is taken.")

    def kill(self):
        """Send a request to the provider to kill the model, if needed."""
        if is_self_hosted_model(self.model):
           self_hosted_model_kill(self)
        logger.debug(f"`LM.kill()` is called for the auto-launched model {self.model} -- no action is taken.")

    def finetune(self, message_completion_pairs: List[Dict[str, str]], config: Dict[str, Any]) -> FinetuneJob:
        """Send a request to the provider to launch the model, if supported."""
        # Fine-tuning is experimental and requires the experimental flag
        from dspy import settings as settings
        err = "Fine-tuning is an experimental feature and requires `dspy.settings.experimental = True`."
        assert settings.experimental, err

        # Find the respective finetuning functions and job classes
        finetune_function = None
        finetune_job = None
        if is_openai_model(self.model):
            finetune_function = finetune_openai
            finetune_job = FinetuneJobOpenAI()
        elif is_anyscale_model(self.model):
            finetune_function = finetune_anyscale
            finetune_function = finetune_anyscale
            finetune_job = FinetuneJobAnyScale()

        # Ensure that the model supports fine-tuning
        if not finetune_function or not finetune_job:
            err = f"Fine-tuning is not supported for the model {self.model}."
            raise ValueError(err)

        # Start asyncronous training
        executor = ThreadPoolExecutor(max_workers=1)
        executor.submit(
            finetune_function,
            finetune_job,
            model=self.model,
            message_completion_pairs=message_completion_pairs,
            config=config
        )
        executor.shutdown(wait=False)

        return finetune_job


@functools.lru_cache(maxsize=None)
def cached_litellm_completion(request):
    return litellm_completion(request, cache={"no-cache": False, "no-store": False})

def litellm_completion(request, cache={"no-cache": True, "no-store": True}):
    kwargs = ujson.loads(request)
    return litellm.completion(cache=cache, **kwargs)

@functools.lru_cache(maxsize=None)
def cached_litellm_text_completion(request):
    return litellm_text_completion(request, cache={"no-cache": False, "no-store": False})

def litellm_text_completion(request, cache={"no-cache": True, "no-store": True}):
    kwargs = ujson.loads(request)

    # Extract the provider and model from the model string.
    model = kwargs.pop("model").split("/", 1)
    provider, model = model[0] if len(model) > 1 else "openai", model[-1]

    # Use the API key and base from the kwargs, or from the environment.
    api_key = kwargs.pop("api_key", None) or os.getenv(f"{provider}_API_KEY")
    api_base = kwargs.pop("api_base", None) or os.getenv(f"{provider}_API_BASE")

    # Build the prompt from the messages.
    prompt = '\n\n'.join([x['content'] for x in kwargs.pop("messages")] + ['BEGIN RESPONSE:'])

    return litellm.text_completion(cache=cache, model=f'text-completion-openai/{model}', api_key=api_key,
                                   api_base=api_base, prompt=prompt, **kwargs)


def _green(text: str, end: str = "\n"):
    return "\x1b[32m" + str(text).lstrip() + "\x1b[0m" + end

def _red(text: str, end: str = "\n"):
    return "\x1b[31m" + str(text) + "\x1b[0m" + end

def _inspect_history(lm, n: int = 1):
    """Prints the last n prompts and their completions."""

    for item in lm.history[-n:]:
        messages = item["messages"] or [{"role": "user", "content": item['prompt']}]
        outputs = item["outputs"]

        print("\n\n\n")
        for msg in messages:
            print(_red(f"{msg['role'].capitalize()} message:"))
            print(msg['content'].strip())
            print("\n")

        print(_red("Response:"))
        print(_green(outputs[0].strip()))

        if len(outputs) > 1:
            choices_text = f" \t (and {len(outputs)-1} other completions)"
            print(_red(choices_text, end=""))
        
    print("\n\n\n")


#-------------------------------------------------------------------------------
#    Functions for supporting self-hosted models
#-------------------------------------------------------------------------------


# TODO: It would be nice to move these to a separate file
def self_hosted_model_launch(lm: LM):
   """Launch a self-hosted model."""
   # TODO: Hardcode logic that starts a local server of choice using a selected
   # server (e.g. VLLM, TGI, SGLang)
   pass


def self_hosted_model_kill(lm: LM):
   """Kill a self-hosted model."""
   # Harcode the logic that kills the local server of choice
   pass
