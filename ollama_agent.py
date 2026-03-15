# =============================================================================
# LANGCHAIN AGENT WITH OLLAMA (local LLM)
#
# Prerequisites:
#   1. Install Ollama:       https://ollama.com/download
#   2. Pull a model:         ollama pull llama3.2
#   3. Install packages:     pip install langchain langchain-ollama langgraph
# =============================================================================

from typing import Annotated
from typing_extensions import TypedDict
from langchain_ollama import ChatOllama
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, AnyMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode


class AgentState(TypedDict):
    # Full conversation history between the user and the LLM.
    # add_messages appends new messages rather than overwriting the list.
    messages: Annotated[list[AnyMessage], add_messages]

    # Python code representing the model space of a CAD file.
    # Populated by the decompose_model_space node before the agent runs.
    model_space_code: str


# -----------------------------------------------------------------------------
# 1. LOCAL LLM via Ollama
#    Make sure Ollama is running: `ollama serve`
#    Change the model name to whichever you have pulled locally.
#    List your models with: `ollama list`
# -----------------------------------------------------------------------------
llm = ChatOllama(
    model="llama3.2",   # e.g. "llama3.2", "mistral", "qwen2.5", "phi3"
    temperature=0,      # 0 = deterministic
    base_url="http://localhost:11434",  # default Ollama address
)


# -----------------------------------------------------------------------------
# 2. TOOLS
#    Add as many tools as you need. The @tool docstring is what the model
#    reads to decide when to use the tool, so be descriptive.
# -----------------------------------------------------------------------------
@tool
def add(a: float, b: float) -> float:
    """Add two numbers together."""
    return a + b


@tool
def multiply(a: float, b: float) -> float:
    """Multiply two numbers together."""
    return a * b


@tool
def get_word_length(word: str) -> int:
    """Return the number of characters in a word."""
    return len(word)


tools = [add, multiply, get_word_length]


# -----------------------------------------------------------------------------
# 3. AGENT
#    Built using LangGraph's StateGraph — the current stable approach.
#    Flow: decompose_model_space → agent → (tools → agent)* → END
# -----------------------------------------------------------------------------

# --- Node 1: decompose model space ---
def decompose_model_space(state: AgentState) -> AgentState:
    """
    Reads the CAD file and converts the model space into a code representation.
    Stores the result in state["model_space_code"] for the agent to reference.

    PSEUDOCODE — replace with real implementation:
        cad_file_path = <get path from state or config>
        raw_model_space = <open and parse the CAD file>
        code_representation = <traverse entities in model space and emit code>
        return {"model_space_code": code_representation}
    """
    # placeholder until real implementation is wired in
    model_space_code = "# TODO: real model space code goes here"
    return {"model_space_code": model_space_code}


system_message = SystemMessage(content="You are a helpful assistant. Use tools when needed.")

llm_with_tools = llm.bind_tools(tools)

def call_model(state: AgentState):
    messages = [system_message] + state["messages"]
    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}

def should_continue(state: AgentState):
    """Route to tools if the model made a tool call, otherwise end."""
    if state["messages"][-1].tool_calls:
        return "tools"
    return END

graph = StateGraph(AgentState)
graph.add_node("decompose_model_space", decompose_model_space)
graph.add_node("agent", call_model)
graph.add_node("tools", ToolNode(tools))
graph.set_entry_point("decompose_model_space")   # always runs first
graph.add_edge("decompose_model_space", "agent") # then hand off to the agent
graph.add_conditional_edges("agent", should_continue)
graph.add_edge("tools", "agent")

agent = graph.compile()


# -----------------------------------------------------------------------------
# 4. RUN
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    questions = [
        "What is 4 multiplied by 7?",
        "How many letters are in the word 'astronaut'?",
        "Add 155 and 278, then multiply the result by 3.",
    ]

    for question in questions:
        print(f"\n{'='*60}")
        print(f"Question: {question}")
        print("="*60)

        result = agent.invoke({
            "messages": [("human", question)],
            "model_space_code": "",   # decompose_model_space node will populate this
        })

        # The final answer is the last message in the response
        print(f"\nFinal Answer: {result['messages'][-1].content}")
