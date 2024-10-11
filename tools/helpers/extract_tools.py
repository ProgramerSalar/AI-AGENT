import re
def extract_tool_requests(response):
    # Regex to match the tool blocks
    pattern = r'<tool\$(.*?)>(.*?)</tool\$>'
    matches = re.findall(pattern, response, re.DOTALL)
    
    tool_usages = []
    
    for match in matches:
        attributes, body = match
        tool_dict = {}
        # Parse attributes
        for attr in re.findall(r'(\w+)="([^"]+)"', attributes):
            tool_dict[attr[0]] = attr[1]
        # Add body content
        tool_dict["body"] = body.strip()
        tool_usages.append(tool_dict)
    
    return tool_usages