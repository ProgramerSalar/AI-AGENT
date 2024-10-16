import os, json, contextlib, time, importlib, inspect
from io import StringIO
from typing import Optional, Dict
from tools.helpers import extract_tools, rate_limiter, files
from tools.helpers.print_style import PrintStyle

from langchain.schema import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage
from langchain_core.language_models.chat_models import BaseChatModel



rate_limit = rate_limiter.rate_limiter(30,80000)

class Agent:
    paused=False
    streaming_agent=None
    @staticmethod
    def configure(model_chat, model_embedding, memory_subdir="", memory_results=3):
        #save configuration
        Agent.model_chat = model_chat
        # initialize memory tool
        from tools import memory_tool
        memory_tool.initialize(
            embeddings_model=model_embedding, 
            messages_returned=memory_results, 
            subdir=memory_subdir )
    
    def __init__(self, system_prompt:Optional[str]=None, tools_prompt:Optional[str]=None, superior:Optional['Agent']=None, number=0):
  
        self.number = number
        self.name = f"Agent {self.number}"
        if system_prompt is None: system_prompt = files.read_file("./prompts/agent.system.md")     
        if tools_prompt is None: tools_prompt = files.read_file("./prompts/agent.tools.md")
        self.system_prompt = system_prompt.replace("{", "{{").replace("}", "}}")
        self.tools_prompt = tools_prompt.replace("{", "{{").replace("}", "}}")
        self.superior: Optional['Agent'] = superior
        
        self.subordinate: Optional['Agent'] = None
        self.history = []
        self.last_message = ""
        self.message_for_superior = ""
        self.intervention_message = ""
        self.intervention_status = False
                
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", self.system_prompt + "\n\n" + self.tools_prompt),
            MessagesPlaceholder(variable_name="messages") ])
    def process_message(self, msg: str):
        try:
            self.append_message(msg, human=True) # Append the user's input to the history
            printer = PrintStyle(italic=True, font_color="#b3ffd9", padding=False)    
            
            while True: # let the agent iterate on his thoughts until he stops by using a tool
                Agent.streaming_agent = self #mark self as current streamer
                agent_response = ""
                self.intervention_status = False # reset interventon status
                try:
                        
                    inputs = {"input": msg,"messages": self.history}
                    chain = self.prompt | Agent.model_chat
                    formatted_inputs = self.prompt.format(**inputs)
                
                    rate_limit(len(formatted_inputs)/4) #wait for rate limiter - A helpful rule of thumb is that one token generally corresponds to ~4 characters of text for common English text. This translates to roughly ¾ of a word (so 100 tokens ~= 75 words).
                    # output that the agent is starting
                    PrintStyle(bold=True, font_color="green", padding=True, background_color="white").print(f"{self.name}: Starting a message:")
                                            
                    for chunk in chain.stream(inputs):
                        if self.handle_intervention(agent_response): break # wait for intervention and handle it, if paused
                         
                        if chunk.content is not None and chunk.content != '':
                            printer.stream(chunk.content) # output the agent response stream                
                            agent_response += chunk.content # type: ignore | concatenate stream into the response
            
                    if not self.handle_intervention(agent_response):
                        #if assistant_response is the same as last message in history, let him know
                        if self.last_message == agent_response:
                            agent_response = files.read_file("./prompts/fw.msg_repeat.md")
                            PrintStyle(font_color="orange", padding=True).print(agent_response)
                        self.last_message = agent_response
                        
                        self.append_message(agent_response) # Append the assistant's response to the history
                        self.process_tools(agent_response)
                        
                        #break the execution if there is a message for superior agent
                        if self.message_for_superior and not self.intervention_status:
                            msg = self.message_for_superior
                            self.message_for_superior = ""
                            return msg            
                    
                # Forward errors to the LLM, maybe he can fix them
                except Exception as e:
                    msg_response = files.read_file("./prompts/fw.error.md", error=str(e)) # error message template
                    self.append_message(msg_response, human=True)
                    PrintStyle(font_color="red", padding=True).print(msg_response)
        finally:
            Agent.streaming_agent = None # unset current streamer
        
    def append_message(self, msg: str, human: bool = False):
        message_type = "human" if human else "ai"
        if self.history and self.history[-1].type == message_type:
            self.history[-1].content += "\n\n" + msg
        else:
            new_message = HumanMessage(content=msg) if human else AIMessage(content=msg)
            self.history.append(new_message)
            self.cleanup_history(5, 10)
        if message_type=="ai":
            self.last_message = msg
    def cleanup_history(self,x, y):
        if len(self.history) <= x + y:
            return self.history
        
        first_x = self.history[:x]
        last_y = self.history[-y:]
        cleanup_prompt = files.read_file("./prompts/fw.msg_cleanup.md")
        middle_values = [AIMessage(content=cleanup_prompt)]
        
        self.history = first_x + middle_values + last_y
    def handle_intervention(self, progress:str="") -> bool:
        while self.paused: time.sleep(0.1) # wait if paused
        if self.intervention_message and not self.intervention_status: # if there is an intervention message, but not yet processed
            if progress.strip(): self.append_message(progress) # append the response generated so far
            user_msg = files.read_file("./prompts/fw.intervention.md", user_message=self.intervention_message) # format the user intervention template
            self.append_message(user_msg,human=True) # append the intervention message
            self.intervention_message = "" # reset the intervention message
            self.intervention_status = True
        return self.intervention_status # return intervention status
    def process_tools(self, msg: str):
        # search for tool usage requests in agent message
        tool_requests = extract_tools.extract_tool_requests(msg)
        
        for tool_request in tool_requests:
            if self.handle_intervention(): break # wait if paused and handle intervention message if needed
            
            tool_name = tool_request["name"]
            tool_function = self.get_tool(tool_name)
            
            if callable(tool_function):
                short_params = {k: v for k, v in tool_request.items() if k != "name" and k != "body"} # only extra parameters to output to console
                PrintStyle(font_color="#1B4F72", padding=True, background_color="white", bold=True).print(f"{self.name}: Using tool {tool_name}:")
                PrintStyle(font_color="#85C1E9").print(short_params, tool_request["body"], sep="\n") if short_params else PrintStyle(font_color="#85C1E9").print(tool_request["body"])
                tool_response = tool_function(self, tool_request["body"], **tool_request) or "" # call tool function with all parameters, body parameter separated for convenience
                Agent.streaming_agent = self # mark self as current streamer again, it may have changed during tool use
                
                if self.handle_intervention(): break # wait if paused and handle intervention message if needed
            
                msg_response = files.read_file("./prompts/fw.tool_response.md", tool_name=tool_name, tool_response=tool_response)
                self.append_message(msg_response, human=True)
                
                PrintStyle(font_color="#1B4F72", background_color="white", padding=True, bold=True).print(f"{self.name}: Response from {tool_name}:")
                PrintStyle(font_color="#85C1E9").print(tool_response)
            else:
                if self.handle_intervention(): break # wait if paused and handle intervention message if needed
                msg_response = files.read_file("./prompts/fw.tool_not_found.md", tool_name=tool_name, tools_prompt=self.tools_prompt)
                self.append_message(msg_response,True)
                PrintStyle(font_color="orange", padding=True).print(msg_response)
    def get_tool(self, name: str):
        module = importlib.import_module("tools." + name)  # Import the module
        functions_list = {name: func for name, func in inspect.getmembers(module, inspect.isfunction)}  # Get all functions in the module
        if "execute" in functions_list: return functions_list["execute"] # Check if the module contains a function named "execute"
        if functions_list: return next(iter(functions_list.values())) # Return the first function if no "execute" function is found
        return None  # Return None if no functions are found
    



