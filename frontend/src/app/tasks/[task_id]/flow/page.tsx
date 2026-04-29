import { TaskFlowGraph } from "@/components/task-flow/TaskFlowGraph";

type PageProps = {
  params: Promise<{ task_id: string }>;
};

export default async function TaskFlowPage({ params }: PageProps) {
  const { task_id: taskId } = await params;
  return (
    <div className="p-4">
      <TaskFlowGraph taskId={taskId} />
    </div>
  );
}
