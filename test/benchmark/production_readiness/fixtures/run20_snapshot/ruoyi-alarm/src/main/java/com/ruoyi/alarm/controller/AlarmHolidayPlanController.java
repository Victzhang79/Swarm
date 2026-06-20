package com.ruoyi.alarm.controller;

import java.util.List;
import org.apache.shiro.authz.annotation.RequiresPermissions;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseBody;
import com.ruoyi.alarm.domain.AlarmHolidayPlan;
import com.ruoyi.alarm.service.IAlarmHolidayPlanService;
import com.ruoyi.common.core.controller.BaseController;
import com.ruoyi.common.core.domain.AjaxResult;
import com.ruoyi.common.core.page.TableDataInfo;

/**
 * 节假日计划Controller
 *
 * @author ruoyi
 */
@Controller
@RequestMapping("/alarm/holiday")
public class AlarmHolidayPlanController extends BaseController
{
    private String prefix = "alarm/holiday";

    @Autowired
    private IAlarmHolidayPlanService holidayPlanService;

    /**
     * 查询节假日计划列表
     */
    @RequiresPermissions("alarm:holiday:list")
    @GetMapping("/list")
    @ResponseBody
    public TableDataInfo list(AlarmHolidayPlan plan)
    {
        startPage();
        List<AlarmHolidayPlan> list = holidayPlanService.selectList(plan);
        return getDataTable(list);
    }

    /**
     * 新增节假日计划
     */
    @RequiresPermissions("alarm:holiday:add")
    @PostMapping("/add")
    @ResponseBody
    public AjaxResult add(AlarmHolidayPlan plan)
    {
        return toAjax(holidayPlanService.insert(plan));
    }

    /**
     * 修改节假日计划
     */
    @RequiresPermissions("alarm:holiday:edit")
    @PostMapping("/edit")
    @ResponseBody
    public AjaxResult edit(AlarmHolidayPlan plan)
    {
        return toAjax(holidayPlanService.update(plan));
    }

    /**
     * 删除节假日计划
     */
    @RequiresPermissions("alarm:holiday:remove")
    @PostMapping("/remove")
    @ResponseBody
    public AjaxResult remove(@RequestParam Long[] planIds)
    {
        return toAjax(holidayPlanService.deleteByIds(planIds));
    }

    /**
     * 批量导入节假日计划
     */
    @RequiresPermissions("alarm:holiday:import")
    @PostMapping("/import")
    @ResponseBody
    public AjaxResult importData(List<AlarmHolidayPlan> plans)
    {
        int count = holidayPlanService.batchImport(plans);
        return AjaxResult.success("成功导入" + count + "条记录");
    }
}
