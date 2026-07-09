from fastmcp import FastMCP

# Initialize the server
mcp = FastMCP("My Simple Server",)

# Define a tool (e.g., a simple calculator)
@mcp.tool()
def add_numbers(a: float, b: float) -> float:
    """Add two numbers together."""
    return a + b

@mcp.tool()
def add_numbers_togggether_with_grtkddgsgffjgdjfgjggchjd(a: float, b: float) -> float:
    """Add two numbers together."""
    return a + b


if __name__ == "__main__":
    # Run using the streamable-http transport
    mcp.run(transport="http", host="0.0.0.0", port=8002, )
