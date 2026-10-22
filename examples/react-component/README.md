# React Component Example

A reusable React TypeScript data table component with sorting, filtering, and loading states. Tests Delegatron with JavaScript/TypeScript projects using a Node.js Docker image.

## What it tests

- TypeScript generics for reusable components
- React state management (sort direction, search filter)
- CSS Modules for scoped styling
- Jest testing with React Testing Library
- Non-Python sandbox (Node.js Docker image)

## How to run

```bash
# Uses node:22-slim Docker image for the sandbox (or pass --docker-image node:22-slim)
export DELEGATRON_DOCKER_IMAGE=node:22-slim
mkdir -p /tmp/react-component          # an empty workspace for the component
delegatron run --workspace /tmp/react-component "$(cat examples/react-component/task.json | jq -r .request)"
```

Without an explicit image the sandbox starts from the default (Python) image
and switches to the Node.js image once the run writes the first `package.json`.

## Success criteria

The run passes if all 8 acceptance criteria are met and Jest tests pass in the Node.js sandbox.
